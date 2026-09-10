"""Fail when the README documents something the code does not do.

Only structural claims can be checked this way: routes, environment
variables, and the files named in the layout table. Prose cannot -- "the
exchange runs on every poll" is a claim about behaviour, and the two drifts
that survived longest here were exactly that kind. This closes the half that
is mechanical; reading closes the rest.

    python tools/check_readme.py
"""

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
README = (ROOT / "README.md").read_text()

# Routes that exist for the framework or a crawler, not for a reader
UNDOCUMENTED_BY_DESIGN = {"/static/<path:filename>", "/apple-touch-icon.png",
                          "/apple-touch-icon-precomposed.png"}
# Variables the deployment platform sets, or that only tooling reads
NOT_APP_CONFIG = {"RSF_TEST_DATABASE_URL", "PATH", "HOME"}


def environment_variables() -> set[str]:
    """Every os.environ key the application reads, found in the AST rather
    than by regex so a renamed variable cannot hide in a string."""
    found = set()
    for path in ROOT.glob("*.py"):
        if path.name.startswith("test_") or path.name == "testing.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            # os.environ["X"]
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                if ast.unparse(node.value).endswith("environ"):
                    found.add(node.slice.value)
            # os.environ.get("X", ...)
            if isinstance(node, ast.Call) and node.args:
                target = ast.unparse(node.func)
                if target.endswith("environ.get") and isinstance(node.args[0], ast.Constant):
                    found.add(node.args[0].value)
    return found - NOT_APP_CONFIG


def failures() -> list[str]:
    problems = []

    import app  # imported late: it opens a connection pool on first use

    live_routes = {r.rule for r in app.app.url_map.iter_rules()} - UNDOCUMENTED_BY_DESIGN
    for route in sorted(live_routes):
        if route not in README:
            problems.append(f"route {route} exists but is not in the README")

    # and the reverse: a documented route that was removed. Scanned only
    # inside the Endpoints table, so prose like "proxies `/api` to Flask" is
    # not mistaken for a claim that a route exists.
    table = re.search(r"## Endpoints\n(.*?)\n## ", README, re.S)
    documented = set(re.findall(r"`(/[\w/<>:.-]*)`", table.group(1) if table else ""))
    documented = {r.split("?")[0] for r in documented}
    live_all = {r.rule for r in app.app.url_map.iter_rules()}
    for route in sorted(documented - live_all):
        problems.append(f"README documents {route}, which no longer exists")

    for variable in sorted(environment_variables()):
        if variable not in README:
            problems.append(f"{variable} is read by the code but not in Configuration")

    # a file may be named relative to the repo root or to the frontend source
    roots = (ROOT, ROOT / "frontend" / "src")
    for path in sorted(re.findall(r"`([\w/]+\.(?:py|tsx?|html))`", README)):
        if not any((root / path).exists() for root in roots):
            problems.append(f"README names {path}, which does not exist")

    return sorted(set(problems))


if __name__ == "__main__":
    found = failures()
    for problem in found:
        print(f"  {problem}")
    print(f"{len(found)} README drift problem(s)" if found else "README matches the code")
    sys.exit(1 if found else 0)
