"""JARVIS front-ends.

* ``gui.web`` - the browser interface (served by ``server.app``): works on a
  phone, on the Pi's own browser and in VS Code port forwarding
* ``gui.desktop`` - an optional Tkinter window for a monitor attached to the Pi

The desktop module is imported lazily so that headless installs never pay for
importing Tkinter.
"""

__all__ = ["desktop"]
