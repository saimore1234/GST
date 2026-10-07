"""Services orchestrate the cloud client, the local store and the sync log.

They receive those three as arguments, so the unit tests run them against an
in-memory fake cloud and fake local store - no site data, no network.
"""
