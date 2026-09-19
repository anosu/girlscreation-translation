"""Read Girls Creation TextAssets and preserve the story's translation context."""

import UnityPy


def text_assets(data: bytes) -> dict[str, bytes]:
    """Recover TextAsset bytes with UnityPy 1.25's surrogateescape encoding."""
    assets = {}
    for obj in UnityPy.load(data).objects:
        if obj.type.name == "TextAsset":
            asset = obj.read()
            if asset.m_Name in assets:
                raise ValueError(f"Duplicate TextAsset: {asset.m_Name}")
            assets[asset.m_Name] = asset.m_Script.encode("utf-8", "surrogateescape")
    if not assets:
        raise ValueError("Bundle contains no TextAssets")
    return assets


def parse_bundle(data: bytes) -> tuple[str, str]:
    """Decode the single text script in a novel bundle."""
    assets = text_assets(data)
    if len(assets) != 1:
        raise ValueError(f"Expected one novel TextAsset, found {len(assets)}")
    name, script = next(iter(assets.items()))
    return name, script.decode("utf-8-sig")


def parse_script(script: str) -> list[dict[str, str | int]]:
    """Keep titles, speakers and line numbers; never rewrite source keys."""
    messages = []
    for number, line in enumerate(script.splitlines(), 1):
        # Keep the game's existing positional delimiter semantics, including literal quotes.
        fields = line.split(",")
        if not fields or fields[0] not in {"title", "message", "msgvoicesync"}:
            continue
        command = fields[0]
        offset = {"title": 1, "message": 2, "msgvoicesync": 3}[command]
        if len(fields) <= offset:
            raise ValueError(f"Malformed {command} at line {number}")
        source = fields[offset]
        if not source:
            continue
        messages.append(
            {
                "kind": "title" if command == "title" else "message",
                "name": "" if command == "title" else fields[offset - 1],
                "message": source,
                "line": number,
            }
        )
    if not messages:
        raise ValueError("Novel contains no translatable commands")
    return messages
