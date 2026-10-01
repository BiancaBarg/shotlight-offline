# Disposable examples

Run these examples only in a separate mayapy process. They create new scenes and must never be executed in an open artist scene.

`demo_scene.py --output /path/to/scratch` builds and lights a synthetic animated toy.

`offline_validation.py --output /path/to/scratch` checks three editable looks, scene preservation, Original and save/reopen, with Python network connections and external-process launching blocked. Native Arnold license watermarks remain visible and prevent exposure assessment.
