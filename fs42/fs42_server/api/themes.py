import os
from fastapi import APIRouter

from fs42 import paths

router = APIRouter(prefix="/about")


@router.get("/themes")
async def get_themes():
    """List available themes.

    Bundled themes plus any the user dropped into their data directory; a
    user theme with the same filename shadows the bundled one, matching how
    /static is served.
    """
    themes = {}
    # Bundled first, user second, so user entries overwrite by id.
    for theme_dir in (paths.static_dir() / "themes", paths.static_overlay("themes")):
        try:
            entries = os.listdir(str(theme_dir))
        except OSError:
            continue
        for file in entries:
            if not file.endswith(".css"):
                continue
            name = file.replace(".css", "")
            display_name = name.replace("_", " ").replace("default", "").strip()
            themes[name] = {
                "id": name,
                "name": (display_name or "Default").title(),
                "path": f"/static/themes/{file}",
            }
    if not themes:
        return {"themes": [{"id": "default", "name": "Default", "path": "/static/themes/default.css"}]}
    return {"themes": sorted(themes.values(), key=lambda x: x["name"])}
