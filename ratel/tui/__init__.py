"""ratel-tui: a read-only terminal console over a channel's Bus.

The pure modules (model, render, slots, data, poll) import no textual so they
test without a running app; only events.py and the app package touch Textual.
"""
