"""A free test tier for owls: owl turns replayed by a scripted model.

A scenario is pure data (``scenarios/*.json``): the owl, the person's messages and actions, what
the "model" answers on each call, fake results for anything that would reach outside, and
checks. ``run.py`` drives the real owls runtime in-process with a fresh store in a temporary
folder, freezes what happened and evaluates the checks on that frozen evidence. No model is
called and nothing leaves the computer. See README.md.
"""
