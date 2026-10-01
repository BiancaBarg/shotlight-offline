# Beta feedback and contributions

Made by Bianca Bargan 😊

Please use the feedback and bug-report issue templates. Useful feedback describes the camera/range, chosen version, elapsed time, subject readability, highlights, shadows, installation and Original restore behaviour.

Include Maya/Arnold versions and operating system, a short reproduction and the visible error text. Use a disposable scene for reproduction. Share screenshots only when you have permission, and remove private scene names, paths or credentials from logs. Do not upload confidential Maya scenes.

The beta currently targets Maya 2025 / Arnold. See VALIDATION.md for tested scope. Local exposure statistics do not constitute artistic approval.

Run `python3 -m unittest discover -s tests -q` for core tests. Native tests and examples create disposable Maya scenes and must run in separate mayapy processes. Do not run them in your open artist session.
