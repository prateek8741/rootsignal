"""What write tools return during replay when the recording has no result for them.

Load with ``rootsignal replay <case> --stubs rootsignal_examples.support_agent.replay_stubs``.
The real tool is never executed.
"""

from rootsignal.replay import stub

stub("create_ticket", lambda summary, priority: {"ticket": "SUP-REPLAY"})
