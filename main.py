"""
Local Expense Tracker
----------------------
100% local, privacy-focused Android expense tracker.
Reads SMS from the native Android inbox via PyJnius, parses financial
transactions (including credit card charges) with regex, stores them in a
local SQLite database, and shows monthly + lifetime summaries in a KivyMD
dashboard. Transactions with no confidently-detected category are queued
and the user is asked to pick one (Rent, Electricity Bill, Food,
Entertainment, Subscriptions, Groceries, Transport, Shopping, Investment,
Salary/Income, Credit Card Payment, Miscellaneous).

No network calls are made anywhere in this app.
"""

import os
import re
import sqlite3
import hashlib
import threading
from functools import partial
from datetime import datetime

from kivy.utils import platform
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.lang import Builder
from kivy.uix.scrollview import ScrollView

from kivymd.app import MDApp
from kivymd.uix.list import TwoLineListItem, OneLineListItem, MDList
from kivymd.uix.dialog import MDDialog
from kivymd.uix.button import MDFlatButton
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.label import MDLabel

# ---------------------------------------------------------------------------
# Android-only imports (guarded so the app can still run on desktop for UI
# development/testing without pyjnius / android permissions being present).
# ---------------------------------------------------------------------------
ANDROID = platform == "android"

if ANDROID:
    from jnius import autoclass, cast  # noqa: F401
    from android.permissions import (
        request_permissions,
        check_permission,
        Permission,
    )

# ---------------------------------------------------------------------------
# Category options offered to the user for manual categorization.
# ---------------------------------------------------------------------------
CATEGORY_OPTIONS = [
    "Rent",
    "Electricity Bill",
    "Food",
    "Entertainment",
    "Subscriptions",
    "Groceries",
    "Transport",
    "Shopping",
    "Investment",
    "Salary/Income",
    "Credit Card Payment",
    "Miscellaneous",
]


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------
class ExpenseDatabase:
    """Thin wrapper around a local SQLite database for transactions."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self):
        # New connection per call: simplest way to be thread-safe with
        # SQLite when the sync happens on a background thread.
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        amount REAL NOT NULL,
                        type TEXT NOT NULL CHECK(type IN ('debit', 'credit')),
                        account_type TEXT NOT NULL DEFAULT 'bank'
                            CHECK(account_type IN ('bank', 'credit_card')),
                        sender TEXT,
                        date TEXT NOT NULL,
                        category TEXT,
                        needs_review INTEGER NOT NULL DEFAULT 0,
                        raw_message TEXT,
                        msg_hash TEXT UNIQUE NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date)"
                )
                # Lightweight migration for DBs created by earlier versions
                # of this app that predate account_type / needs_review.
                for ddl in (
                    "ALTER TABLE transactions ADD COLUMN account_type TEXT NOT NULL DEFAULT 'bank'",
                    "ALTER TABLE transactions ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0",
                ):
                    try:
                        conn.execute(ddl)
                    except sqlite3.OperationalError:
                        pass  # column already exists
                conn.commit()
            finally:
                conn.close()

    def add_transaction(self, amount, tx_type, account_type, sender, date_str,
                         category, needs_review, raw_message):
        """Insert a transaction. Returns the new row id if inserted, or
        None if it was a duplicate (already present)."""
        msg_hash = hashlib.sha256(
            f"{sender}|{date_str}|{amount:.2f}|{tx_type}|{account_type}".encode("utf-8")
        ).hexdigest()

        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO transactions
                        (amount, type, account_type, sender, date, category,
                         needs_review, raw_message, msg_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (amount, tx_type, account_type, sender, date_str, category,
                     int(needs_review), raw_message, msg_hash),
                )
                conn.commit()
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                # Duplicate message hash -> already recorded, skip silently.
                return None
            finally:
                conn.close()

    def update_category(self, tx_id, category):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE transactions SET category = ?, needs_review = 0 WHERE id = ?",
                    (category, tx_id),
                )
                conn.commit()
            finally:
                conn.close()

    def get_monthly_summary(self, year_month):
        """year_month format: 'YYYY-MM'. Returns (income, expense, net)."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """
                    SELECT type, COALESCE(SUM(amount), 0)
                    FROM transactions
                    WHERE strftime('%Y-%m', date) = ?
                    GROUP BY type
                    """,
                    (year_month,),
                ).fetchall()
            finally:
                conn.close()

        income = 0.0
        expense = 0.0
        for tx_type, total in rows:
            if tx_type == "credit":
                income = total
            elif tx_type == "debit":
                expense = total
        return income, expense, income - expense

    def get_monthly_card_spend(self, year_month):
        """Total credit-card spend (debit) for the given month."""
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0)
                    FROM transactions
                    WHERE strftime('%Y-%m', date) = ?
                      AND account_type = 'credit_card'
                      AND type = 'debit'
                    """,
                    (year_month,),
                ).fetchone()
            finally:
                conn.close()
        return row[0] if row else 0.0

    def get_lifetime_balance(self):
        """Returns (total_income, total_expense, net_balance) across all time."""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """
                    SELECT type, COALESCE(SUM(amount), 0)
                    FROM transactions
                    GROUP BY type
                    """
                ).fetchall()
            finally:
                conn.close()

        income = 0.0
        expense = 0.0
        for tx_type, total in rows:
            if tx_type == "credit":
                income = total
            elif tx_type == "debit":
                expense = total
        return income, expense, income - expense

    def get_recent_transactions(self, limit=50):
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """
                    SELECT amount, type, account_type, sender, date, category, needs_review
                    FROM transactions
                    ORDER BY date DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
        return rows

    def get_transactions_for_month(self, year_month):
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """
                    SELECT amount, type, account_type, sender, date, category, needs_review
                    FROM transactions
                    WHERE strftime('%Y-%m', date) = ?
                    ORDER BY date DESC
                    """,
                    (year_month,),
                ).fetchall()
            finally:
                conn.close()
        return rows

    def get_pending_review(self, limit=200):
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """
                    SELECT id, amount, sender, raw_message
                    FROM transactions
                    WHERE needs_review = 1
                    ORDER BY date DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
        return rows


# ---------------------------------------------------------------------------
# SMS parsing engine
# ---------------------------------------------------------------------------
class SMSParser:
    """Extracts financial transaction data out of raw SMS text using regex
    heuristics, and filters out OTPs / promotional / non-financial SMS."""

    # Currency-prefixed amount, e.g. "Rs. 1,250.50", "INR 500", "$45.00"
    AMOUNT_PATTERN_PREFIX = re.compile(
        r"(?:rs\.?|inr|₹|usd|\$)\s*([0-9]+(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?)",
        re.IGNORECASE,
    )
    # Currency-suffixed amount, e.g. "500 INR", "1,200.00 Rs"
    AMOUNT_PATTERN_SUFFIX = re.compile(
        r"([0-9]+(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?)\s*(?:rs\.?|inr|₹)",
        re.IGNORECASE,
    )

    DEBIT_KEYWORDS = [
        "debited", "debit", "spent", "paid", "withdrawn", "withdrawal",
        "deducted", "purchase of", "purchased", "sent to", "transferred to",
        "charged",
    ]
    CREDIT_KEYWORDS = [
        "credited", "credit", "received", "deposited", "refund",
        "refunded", "added to your account", "cashback",
    ]

    # If any of these appear, the message is not a real transaction record.
    EXCLUDE_KEYWORDS = [
        "otp", "one time password", "verification code", "do not share",
        "will expire in", "valid for", "sale", "offer", "discount",
        "cashback offer", "click here", "download now", "subscribe now",
        "unsubscribe", "win ", "lucky draw", "limited period",
        "apply now", "loan offer", "pre-approved",
    ]

    # Presence of any of these means the transaction happened on a credit
    # card rather than a savings/current bank account.
    CREDIT_CARD_KEYWORDS = [
        "credit card", "card ending", "card no.", "card no ", "card xx",
        "your card ending", "cc no", "on your hdfc bank card",
        "on your card", "credit card no",
    ]

    # Messages about paying off / reversing a card charge - these should
    # keep their detected type instead of being force-flipped to debit.
    CARD_REFUND_KEYWORDS = [
        "refund", "refunded", "reversed", "reversal", "cashback",
        "waived", "credited back",
    ]

    CATEGORY_KEYWORDS = {
        "Food": ["swiggy", "zomato", "restaurant", "food", "cafe", "dominos", "starbucks"],
        "Shopping": ["amazon", "flipkart", "myntra", "shopping", "mall", "store"],
        "Transport": ["uber", "ola", "rapido", "irctc", "fuel", "petrol", "diesel", "metro"],
        "Electricity Bill": ["electricity", "power bill", "discom", "electricity bill"],
        "Rent": ["rent payment", "house rent", "rent paid", "towards rent"],
        "Subscriptions": ["netflix", "spotify", "prime video", "hotstar", "subscription", "youtube premium"],
        "Entertainment": ["movie", "bookmyshow", "pvr", "inox", "cinema"],
        "Groceries": ["grocery", "bigbasket", "blinkit", "zepto", "supermarket"],
        "Investment": ["mutual fund", "sip", "stocks", "zerodha", "groww", "upstox"],
        "Salary/Income": ["salary", "payroll", "income", "interest credited"],
        "Credit Card Payment": ["credit card bill", "card bill payment", "towards your credit card"],
    }

    @classmethod
    def _extract_amount(cls, body):
        match = cls.AMOUNT_PATTERN_PREFIX.search(body)
        if not match:
            match = cls.AMOUNT_PATTERN_SUFFIX.search(body)
        if not match:
            return None
        raw = match.group(1).replace(",", "")
        try:
            return round(float(raw), 2)
        except ValueError:
            return None

    @classmethod
    def _extract_type(cls, body_lower):
        is_debit = any(kw in body_lower for kw in cls.DEBIT_KEYWORDS)
        is_credit = any(kw in body_lower for kw in cls.CREDIT_KEYWORDS)
        if is_debit and not is_credit:
            return "debit"
        if is_credit and not is_debit:
            return "credit"
        if is_debit and is_credit:
            # Ambiguous message (contains both words) - prefer whichever
            # keyword appears first, closer to the start of the message.
            debit_pos = min(
                (body_lower.find(kw) for kw in cls.DEBIT_KEYWORDS if kw in body_lower),
                default=-1,
            )
            credit_pos = min(
                (body_lower.find(kw) for kw in cls.CREDIT_KEYWORDS if kw in body_lower),
                default=-1,
            )
            if debit_pos == -1:
                return "credit"
            if credit_pos == -1:
                return "debit"
            return "debit" if debit_pos < credit_pos else "credit"
        return None

    @classmethod
    def _categorize(cls, body_lower, sender_lower):
        for category, keywords in cls.CATEGORY_KEYWORDS.items():
            for kw in keywords:
                if kw in body_lower or kw in sender_lower:
                    return category
        return None  # unknown -> caller will ask the user

    @classmethod
    def parse(cls, address, body, date_ms):
        """Returns a dict with amount/type/account_type/date/category/sender
        if this SMS is a genuine financial transaction, else returns None."""
        if not body:
            return None

        body_lower = body.lower()
        sender_lower = (address or "").lower()

        # Filter out OTPs / promotional / spam messages up front.
        if any(kw in body_lower for kw in cls.EXCLUDE_KEYWORDS):
            return None

        amount = cls._extract_amount(body)
        if amount is None or amount <= 0:
            return None

        tx_type = cls._extract_type(body_lower)
        if tx_type is None:
            return None

        is_credit_card = any(kw in body_lower for kw in cls.CREDIT_CARD_KEYWORDS)
        account_type = "credit_card" if is_credit_card else "bank"

        # Many card issuers phrase a purchase as "Rs.500 credited to your
        # card ending 1234" - that is a charge (an expense you now owe),
        # not income, so flip it to a debit unless it's clearly a refund
        # or a reversal being credited back to the card.
        if account_type == "credit_card" and tx_type == "credit":
            if not any(kw in body_lower for kw in cls.CARD_REFUND_KEYWORDS):
                tx_type = "debit"

        try:
            date_str = datetime.fromtimestamp(date_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        category = cls._categorize(body_lower, sender_lower)

        return {
            "amount": amount,
            "type": tx_type,
            "account_type": account_type,
            "sender": address or "Unknown",
            "date": date_str,
            "category": category,  # may be None -> needs user input
            "raw_message": body,
        }


# ---------------------------------------------------------------------------
# Android SMS reader (PyJnius)
# ---------------------------------------------------------------------------
def read_sms_inbox():
    """Reads all messages from content://sms/inbox on the device.
    Returns a list of dicts: {'address', 'body', 'date_ms'}.
    Returns an empty list (and never raises) on non-Android platforms or
    on any native error."""
    if not ANDROID:
        return []

    messages = []
    try:
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        activity = PythonActivity.mActivity
        content_resolver = activity.getContentResolver()
        Uri = autoclass("android.net.Uri")
        sms_uri = Uri.parse("content://sms/inbox")

        projection = ["address", "body", "date"]
        cursor = content_resolver.query(sms_uri, projection, None, None, "date DESC")

        if cursor is not None:
            try:
                if cursor.getCount() > 0 and cursor.moveToFirst():
                    address_idx = cursor.getColumnIndex("address")
                    body_idx = cursor.getColumnIndex("body")
                    date_idx = cursor.getColumnIndex("date")
                    while True:
                        address = cursor.getString(address_idx) if address_idx >= 0 else ""
                        body = cursor.getString(body_idx) if body_idx >= 0 else ""
                        date_ms = cursor.getLong(date_idx) if date_idx >= 0 else 0
                        messages.append(
                            {"address": address, "body": body, "date_ms": date_ms}
                        )
                        if not cursor.moveToNext():
                            break
            finally:
                cursor.close()
    except Exception as exc:  # noqa: BLE001 - surface any native error
        print(f"[SMS READ ERROR] {exc}")
        return []

    return messages


# ---------------------------------------------------------------------------
# UI (KivyMD)
# ---------------------------------------------------------------------------
KV = """
MDScreen:
    MDBoxLayout:
        orientation: "vertical"

        MDToolbar:
            id: toolbar
            title: "Expense Tracker"
            elevation: 4
            left_action_items: [["wallet-outline", lambda x: None]]

        ScrollView:
            do_scroll_x: False

            MDBoxLayout:
                orientation: "vertical"
                adaptive_height: True
                padding: dp(16)
                spacing: dp(16)

                MDCard:
                    orientation: "vertical"
                    padding: dp(16)
                    spacing: dp(8)
                    size_hint_y: None
                    height: dp(150)
                    elevation: 2
                    radius: [12, 12, 12, 12]

                    MDLabel:
                        id: month_label
                        text: "This Month"
                        font_style: "Subtitle1"
                        bold: True
                        size_hint_y: None
                        height: dp(24)

                    MDBoxLayout:
                        orientation: "horizontal"
                        spacing: dp(8)

                        MDBoxLayout:
                            orientation: "vertical"
                            MDLabel:
                                text: "Income"
                                theme_text_color: "Secondary"
                                font_style: "Caption"
                            MDLabel:
                                id: month_income_label
                                text: "0.00"
                                bold: True

                        MDBoxLayout:
                            orientation: "vertical"
                            MDLabel:
                                text: "Expense"
                                theme_text_color: "Secondary"
                                font_style: "Caption"
                            MDLabel:
                                id: month_expense_label
                                text: "0.00"
                                bold: True

                        MDBoxLayout:
                            orientation: "vertical"
                            MDLabel:
                                text: "Net Savings"
                                theme_text_color: "Secondary"
                                font_style: "Caption"
                            MDLabel:
                                id: month_net_label
                                text: "0.00"
                                bold: True

                MDCard:
                    orientation: "vertical"
                    padding: dp(16)
                    spacing: dp(8)
                    size_hint_y: None
                    height: dp(90)
                    elevation: 2
                    radius: [12, 12, 12, 12]

                    MDLabel:
                        text: "Lifetime Balance"
                        theme_text_color: "Secondary"
                        font_style: "Caption"

                    MDLabel:
                        id: lifetime_balance_label
                        text: "0.00"
                        font_style: "H5"
                        bold: True

                MDCard:
                    orientation: "vertical"
                    padding: dp(16)
                    spacing: dp(8)
                    size_hint_y: None
                    height: dp(90)
                    elevation: 2
                    radius: [12, 12, 12, 12]

                    MDLabel:
                        text: "Credit Card Spend (This Month)"
                        theme_text_color: "Secondary"
                        font_style: "Caption"

                    MDLabel:
                        id: credit_card_spend_label
                        text: "0.00"
                        font_style: "H5"
                        bold: True

                MDBoxLayout:
                    orientation: "horizontal"
                    spacing: dp(12)
                    size_hint_y: None
                    height: dp(48)
                    pos_hint: {"center_x": 0.5}

                    MDRaisedButton:
                        id: sync_button
                        text: "Sync SMS"
                        icon: "sync"
                        on_release: app.sync_sms()

                    MDRaisedButton:
                        id: review_button
                        text: "Review Uncategorized"
                        icon: "tag-outline"
                        on_release: app.review_uncategorized()

                MDLabel:
                    text: "Recent Transactions"
                    font_style: "Subtitle1"
                    bold: True
                    size_hint_y: None
                    height: dp(28)

                MDList:
                    id: transactions_list
"""


class ExpenseTrackerApp(MDApp):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.db = None
        self._dialog = None
        self._review_queue = []

    def build(self):
        self.theme_cls.theme_style = "Light"
        self.theme_cls.primary_palette = "Teal"
        return Builder.load_string(KV)

    def on_start(self):
        db_dir = self.user_data_dir
        try:
            os.makedirs(db_dir, exist_ok=True)
        except OSError:
            pass
        db_path = os.path.join(db_dir, "expenses.db")
        self.db = ExpenseDatabase(db_path)

        self._request_permissions()
        self.refresh_dashboard()

    # -- Permissions --------------------------------------------------------
    def _request_permissions(self):
        if not ANDROID:
            return
        try:
            needed = [Permission.READ_SMS, Permission.RECEIVE_SMS]
            missing = [p for p in needed if not check_permission(p)]
            if missing:
                request_permissions(missing, self._on_permissions_result)
        except Exception as exc:  # noqa: BLE001
            self._show_dialog("Permission Error", str(exc))

    def _on_permissions_result(self, permissions, grants):
        if not all(grants):
            Clock.schedule_once(
                lambda dt: self._show_dialog(
                    "Permission Required",
                    "SMS read permission is required to automatically "
                    "detect transactions. You can grant it from the app "
                    "settings and tap 'Sync SMS' again.",
                )
            )

    # -- Sync -----------------------------------------------------------------
    def sync_sms(self):
        if ANDROID:
            has_perm = check_permission(Permission.READ_SMS)
            if not has_perm:
                self._request_permissions()
                self._show_dialog(
                    "Permission Needed",
                    "Please grant SMS permission, then tap 'Sync SMS' again.",
                )
                return

        self.root.ids.sync_button.disabled = True
        self.root.ids.sync_button.text = "Syncing..."
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self):
        review_queue = []
        try:
            messages = read_sms_inbox()
            inserted = 0
            skipped = 0
            for msg in messages:
                parsed = SMSParser.parse(
                    msg.get("address", ""), msg.get("body", ""), msg.get("date_ms", 0)
                )
                if parsed is None:
                    continue

                needs_review = parsed["category"] is None
                final_category = parsed["category"] or "Uncategorized"

                tx_id = self.db.add_transaction(
                    parsed["amount"],
                    parsed["type"],
                    parsed["account_type"],
                    parsed["sender"],
                    parsed["date"],
                    final_category,
                    needs_review,
                    parsed["raw_message"],
                )
                if tx_id is not None:
                    inserted += 1
                    if needs_review:
                        review_queue.append(
                            {
                                "id": tx_id,
                                "amount": parsed["amount"],
                                "sender": parsed["sender"],
                                "raw_message": parsed["raw_message"],
                            }
                        )
                else:
                    skipped += 1

            result_text = f"Added {inserted} new transaction(s)."
            if skipped:
                result_text += f" ({skipped} duplicate(s) skipped)"
            if not messages and ANDROID:
                result_text = "No SMS messages were found on this device."
        except Exception as exc:  # noqa: BLE001
            result_text = f"Sync failed: {exc}"

        Clock.schedule_once(lambda dt: self._on_sync_done(result_text, review_queue))

    def _on_sync_done(self, result_text, review_queue):
        self.root.ids.sync_button.disabled = False
        self.root.ids.sync_button.text = "Sync SMS"
        self.refresh_dashboard()

        if review_queue:
            self._review_queue = review_queue
            self._show_dialog(
                "Sync Complete",
                f"{result_text}\n\n{len(review_queue)} transaction(s) need a "
                f"category - you'll be asked next.",
            )
            Clock.schedule_once(lambda dt: self._show_next_category_prompt(), 0.6)
        else:
            self._show_dialog("Sync Complete", result_text)

    # -- Manual review of anything still uncategorized ------------------------
    def review_uncategorized(self):
        rows = self.db.get_pending_review(limit=200)
        if not rows:
            self._show_dialog("All Set", "No transactions currently need a category.")
            return
        self._review_queue = [
            {"id": r[0], "amount": r[1], "sender": r[2], "raw_message": r[3] or ""}
            for r in rows
        ]
        self._show_next_category_prompt()

    def _show_next_category_prompt(self):
        if not self._review_queue:
            self.refresh_dashboard()
            return
        item = self._review_queue.pop(0)
        self._build_category_dialog(item)

    def _build_category_dialog(self, item):
        if self._dialog:
            self._dialog.dismiss()

        container = MDBoxLayout(
            orientation="vertical", spacing=dp(8), size_hint_y=None, height=dp(360)
        )

        preview_text = (
            f"{item['sender']}\nRs. {item['amount']:,.2f}\n"
            f"{(item['raw_message'] or '')[:140]}"
        )
        container.add_widget(
            MDLabel(text=preview_text, size_hint_y=None, height=dp(80))
        )

        scroll = ScrollView(size_hint=(1, None), height=dp(270))
        category_list = MDList()
        for cat in CATEGORY_OPTIONS:
            category_list.add_widget(
                OneLineListItem(
                    text=cat,
                    on_release=partial(self._select_category, item["id"], cat),
                )
            )
        scroll.add_widget(category_list)
        container.add_widget(scroll)

        self._dialog = MDDialog(
            title="What is this transaction for?",
            type="custom",
            content_cls=container,
            auto_dismiss=False,
        )
        self._dialog.open()

    def _select_category(self, tx_id, category, *_args):
        self.db.update_category(tx_id, category)
        if self._dialog:
            self._dialog.dismiss()
        Clock.schedule_once(lambda dt: self._show_next_category_prompt(), 0.2)

    # -- Dashboard rendering ----------------------------------------------------
    def refresh_dashboard(self):
        if self.db is None:
            return

        now = datetime.now()
        year_month = now.strftime("%Y-%m")
        month_name = now.strftime("%B %Y")

        income, expense, net = self.db.get_monthly_summary(year_month)
        life_income, life_expense, life_net = self.db.get_lifetime_balance()
        card_spend = self.db.get_monthly_card_spend(year_month)

        ids = self.root.ids
        ids.month_label.text = f"This Month ({month_name})"
        ids.month_income_label.text = f"{income:,.2f}"
        ids.month_expense_label.text = f"{expense:,.2f}"
        ids.month_net_label.text = f"{net:,.2f}"
        ids.lifetime_balance_label.text = f"{life_net:,.2f}"
        ids.credit_card_spend_label.text = f"{card_spend:,.2f}"

        ids.transactions_list.clear_widgets()
        recent = self.db.get_recent_transactions(limit=50)
        for amount, tx_type, account_type, sender, date_str, category, needs_review in recent:
            sign = "+" if tx_type == "credit" else "-"
            card_tag = "[Card] " if account_type == "credit_card" else ""
            review_tag = "  (needs category)" if needs_review else ""
            item = TwoLineListItem(
                text=f"{card_tag}{sign} {amount:,.2f}  ({category}){review_tag}",
                secondary_text=f"{sender} | {date_str}",
            )
            ids.transactions_list.add_widget(item)

    # -- Helpers ----------------------------------------------------------------
    def _show_dialog(self, title, text):
        if self._dialog:
            self._dialog.dismiss()
        self._dialog = MDDialog(
            title=title,
            text=text,
            buttons=[MDFlatButton(text="OK", on_release=lambda x: self._dialog.dismiss())],
        )
        self._dialog.open()


if __name__ == "__main__":
    ExpenseTrackerApp().run()
