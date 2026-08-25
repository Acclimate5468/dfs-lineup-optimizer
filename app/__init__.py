"""Streamlit app package.

This ``__init__`` exists so the Streamlit pages can import the small shared
navigation helper :mod:`app.prototype_mode` (the legacy-page "prototype lock").
It declares no app state and runs no Streamlit code on import. The Streamlit
entrypoint is still ``app/streamlit_app.py`` and the pages still live in
``app/pages/`` — this package marker does not change how Streamlit discovers or
runs them.
"""
