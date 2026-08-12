[app]
title = Expense Tracker
package.name = expensetracker
package.domain = org.nitish
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,ttf
version = 1.0
requirements = python3,kivy==2.2.1,kivymd==1.1.1,pyjnius,android,sqlite3
orientation = portrait
fullscreen = 0
android.permissions = READ_SMS,RECEIVE_SMS
android.api = 33
android.minapi = 21
android.ndk = 25b
android.sdk = 33
android.archs = arm64-v8a, armeabi-v7a
android.allow_backup = True
android.accept_sdk_license = True
p4a.branch = master

[buildozer]
log_level = 2
warn_on_root = 1
