"""Template context processor (PLAN T30): cheap, globally-available nav
header vars so `base.html`'s shared header doesn't need every single view
to thread the same things through by hand (unresolved dupe count, latest
known update). Deliberately minimal -- cheap reads, no per-page
filtering/query data lives here.

Registered in `settings.py`'s `TEMPLATES[0]["OPTIONS"]["context_processors"]`
(flagged per T30 brief: the one addition to settings.py this task makes).

T33: dropped `nav_working_from`/`nav_working_to` -- the header's "Working:
X -> Y" chip they fed was removed (the grid's own From/To filter is now the
single, persistent source of the working range; see grid.html).
"""

from . import phaseb, updates


def nav_context(request):
    return {
        "nav_unresolved_pair_count": phaseb.unresolved_pair_count(),
        "nav_update_info": updates.latest_known_update(),
    }
