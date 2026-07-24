"""Web front end and sync server for the VRChat mod suite.

Runs against the same modtool.db the desktop app uses, so incidents and age
checks are one shared record set whether they were filed from the Tkinter app
or the browser. See webapp/server.py for the routes and README-web.md for setup.
"""
