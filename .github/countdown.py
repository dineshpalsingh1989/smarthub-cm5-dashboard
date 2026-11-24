"""Draws the README's "V2.0 in N days, H hours" banner. Run hourly by .github/workflows/countdown.yml.

Usage: python3 .github/countdown.py OUT.svg   (stdlib only)
"""
import datetime, sys

MYT = datetime.timezone(datetime.timedelta(hours=8), "MYT")  # Malaysia time
RELEASE = datetime.datetime(2026, 12, 15, 10, 0, tzinfo=MYT)
START = datetime.datetime(2026, 11, 24, 0, 0, tzinfo=MYT)    # countdown announced: progress bar starts here
WHEN = "15 December 2026 · 10:00 AM (Malaysia time)"


def parts(now):  # -> list of (number, label) boxes, or None once released
    left = RELEASE - now
    if left.total_seconds() <= 0:
        return None
    d, h = left.days, left.seconds // 3600
    if not d and not h:  # last hour
        return [("0", "DAYS"), ("&lt;1", "HOUR")]
    return [(str(d), "DAY" if d == 1 else "DAYS"), (str(h), "HOUR" if h == 1 else "HOURS")]


def box(x, w, big, label):
    n = len(big.replace("&lt;", "<"))  # visible length
    size = 52 if n <= 2 else 40 if n == 3 else 32
    return f"""  <rect x="{x}" y="22" width="{w}" height="106" rx="16" fill="#1d2330"/>
  <text x="{x + w / 2:.0f}" y="{70 + size * .35:.0f}" font-size="{size}" font-weight="800" fill="#fff" text-anchor="middle">{big}</text>
  <text x="{x + w / 2:.0f}" y="116" font-size="13" font-weight="800" fill="#ffd23f" text-anchor="middle" letter-spacing="2">{label}</text>
"""


def svg(now):
    p = parts(now)
    if p:
        boxes = box(168, 104, *p[0]) + box(282, 104, *p[1])
        sub, btn, foot = "releases " + WHEN, "Try the beta now →", "Beta testing · updated hourly"
    else:
        boxes = box(168, 218, "V2.0", "IS OUT")
        sub, btn, foot = "released " + WHEN, "Get it →", "Now the stable version"
    done = min(1, max(0, (now - START) / (RELEASE - START)))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 150" font-family="Segoe UI,Helvetica,Arial,sans-serif">
  <rect x="2" y="2" width="796" height="146" rx="22" fill="#EAF5FC" stroke="#cfe6f5" stroke-width="3"/>
  <g transform="translate(26 42) scale(.5)">
    <rect x="-12" y="138" width="224" height="14" rx="5" fill="#3d434d" stroke="#1d2330" stroke-width="3"/>
    <rect x="160" y="-30" width="16" height="80" rx="8" fill="#5b616c" stroke="#1d2330" stroke-width="4"/>
    <path d="M176 -28h60l-14 20l14 20h-60z" fill="#ffd23f" stroke="#1d2330" stroke-width="4" stroke-linejoin="round"/>
    <text x="200" y="-1" font-size="22" font-weight="800" fill="#1d2330" text-anchor="middle">V2</text>
    <rect x="0" y="30" width="200" height="115" rx="20" fill="#2b2f36" stroke="#1d2330" stroke-width="4"/>
    <path d="M22 50h156M22 62h156" stroke="#3f4550" stroke-width="5" stroke-linecap="round"/>
    <ellipse cx="70" cy="95" rx="11" ry="13" fill="#fff"/><ellipse cx="130" cy="95" rx="11" ry="13" fill="#fff"/>
    <circle cx="74" cy="97" r="5.5" fill="#1d2330"/><circle cx="134" cy="97" r="5.5" fill="#1d2330"/>
    <path d="M80 112q20 18 40 0" fill="#fff" stroke="#fff" stroke-width="3" stroke-linejoin="round"/>
    <circle cx="48" cy="114" r="7" fill="#ff8fa3" opacity=".6"/><circle cx="152" cy="114" r="7" fill="#ff8fa3" opacity=".6"/>
  </g>
{boxes}  <text x="408" y="50" font-size="21" font-weight="800" fill="#1d2330">SmartHub Dashboard V2.0</text>
  <text x="408" y="73" font-size="13" fill="#6b7684">{sub}</text>
  <rect x="408" y="86" width="370" height="10" rx="5" fill="#d5dbe2"/>
  <rect x="408" y="86" width="{370 * done:.0f}" height="10" rx="5" fill="#1E9AD6"/>
  <text x="408" y="125" font-size="11.5" fill="#8d99a8">{foot}</text>
  <rect x="640" y="106" width="140" height="30" rx="15" fill="#1E9AD6" stroke="#1d2330" stroke-width="2"/>
  <text x="710" y="126" font-size="12.5" font-weight="800" fill="#fff" text-anchor="middle">{btn}</text>
</svg>
"""


def selftest():
    at = lambda *a: datetime.datetime(*a, tzinfo=MYT)
    assert parts(at(2026, 11, 24, 7, 30)) == [("21", "DAYS"), ("2", "HOURS")]
    assert parts(at(2026, 12, 14, 9, 0)) == [("1", "DAY"), ("1", "HOUR")]
    assert parts(at(2026, 12, 15, 9, 59)) == [("0", "DAYS"), ("&lt;1", "HOUR")]  # last hour
    assert parts(RELEASE) is None and "IS OUT" in svg(RELEASE)
    assert 'width="0"' in svg(START) and 'width="370"' in svg(RELEASE + datetime.timedelta(days=9))


if __name__ == "__main__":
    selftest()
    open(sys.argv[1], "w", encoding="utf-8").write(svg(datetime.datetime.now(MYT)))
