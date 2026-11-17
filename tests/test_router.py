"""Wake rules (design section 6)."""

from agent_hub.core.router import decide, parse_mentions

AGENTS = ["claude", "hermes", "grok"]
MEMBERS = ["owner", *AGENTS]


def room(sender, mentions=(), mode="mention", **kw):
    return decide(sender=sender, sender_is_owner=sender == "owner", kind="room", mode=mode, members=MEMBERS,
                  agents=AGENTS, mentions=list(mentions), **kw)


def test_parse_mentions():
    assert parse_mentions("@claude and @Hermes, not @nobody or a@claude.com", MEMBERS) == ["claude", "hermes"]
    assert parse_mentions("@all please, @claude first", MEMBERS) == ["all", "claude"]
    assert parse_mentions("no mention", MEMBERS) == []


def test_dm_wakes_the_other_agent_and_never_the_owner():
    def dm(sender, members):
        return decide(sender=sender, sender_is_owner=sender == "owner", kind="dm", mode="dm",
                      members=members, agents=[m for m in members if m != "owner"], mentions=[]).targets
    assert dm("owner", ["claude", "owner"]) == ["claude"]
    assert dm("claude", ["claude", "owner"]) == []
    assert dm("claude", ["claude", "grok"]) == ["grok"]


def test_room_mention_wakes_only_the_mentioned():
    assert room("owner", ["claude"]).targets == ["claude"]
    assert room("claude", ["grok", "claude"]).targets == ["grok"]      # never itself
    assert room("owner").targets == []                                  # mode `mention`: no wake


def test_owner_all_fans_out_in_roster_order():
    plan = room("owner", ["all"])
    assert (plan.targets, plan.fanout) == (AGENTS, "sequential")
    assert room("owner", ["all"], fanout="parallel").fanout == "parallel"


def test_agent_all_wakes_nobody_unless_configured():
    assert room("claude", ["all"]).targets == []
    assert room("claude", ["all"], agent_all=True).targets == ["hermes", "grok"]
    assert room("claude", ["all", "grok"]).targets == ["grok"]         # the explicit mention still counts


def test_round_robin_takes_the_next_agent_in_roster_order():
    assert room("owner", mode="round_robin").targets == ["claude"]
    assert room("owner", mode="round_robin", last_agent_sender="claude").targets == ["hermes"]
    assert room("claude", mode="round_robin").targets == ["hermes"]
    assert room("grok", mode="round_robin").targets == ["claude"]      # wraps


def test_moderated_wakes_the_moderator():
    assert room("owner", mode="moderated", moderator="hermes").targets == ["hermes"]
    assert room("hermes", mode="moderated", moderator="hermes").targets == []
    assert room("owner", ["grok"], mode="moderated", moderator="hermes").targets == ["grok"]
