# Surebet Lab

Local research prototype for odds monitoring and manual workflow. Does not place bets. The application is currently restricted to localhost and is not configured or security-reviewed for public deployment.

Start locally: `python3 work/local_server.py` and visit http://localhost:8765/surebet-lab.html. Provider credentials are kept outside the repository. Accounts and working data are local SQLite data and are not included.

A hosted deployment needs persistent storage, secured credentials, proper authentication and origin controls, and an always-on feed process. The current polling happens only while the browser is open. Do not expose this prototype publicly as-is.
