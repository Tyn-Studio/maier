"""Template context processor (PLAN T30): cheap, globally-available nav
header vars so `base.html`'s shared header doesn't need every single view
to thread the same three things through by hand (unresolved dupe count,
working date range, latest known update). Deliberately minimal -- three
cheap reads, no per-page filtering/query data lives here.

Registered in `settings.py`'s `TEMPLATES[0]["OPTIONS"]["context_processors"]`
(flagged per T30 brief: the one addition to settings.py this task makes).
"""

from django.conf import settings as django_settings

from . import folder_settings, phaseb, updates


def nav_context(request):
    folder = django_settings.WORKING_FOLDER
    current = folder_settings.load_settings(folder)
    return {
        "nav_unresolved_pair_count": phaseb.unresolved_pair_count(),
        "nav_working_from": current.working_from,
        "nav_working_to": current.working_to,
        "nav_update_info": updates.latest_known_update(),
    }
