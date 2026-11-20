"""Screen observation and mouse and keyboard input on Windows.

``backend.py`` defines what the tools need from the operating system, ``win32.py``
implements it with the Windows API through ``ctypes`` (no extra dependency), ``png.py``
encodes screenshots, and ``tools.py`` defines the MCP tools and their safeguards. Nothing
here is imported unless ``[screen]`` or ``[input]`` is enabled in the config file.
"""
