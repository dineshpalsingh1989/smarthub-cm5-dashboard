"""Draws the README's "V2.0 in N days" banner. Run daily by .github/workflows/countdown.yml.

Usage: python3 .github/countdown.py OUT.svg   (stdlib only)
"""
import datetime, sys

RELEASE = datetime.date(2026, 12, 15)
START = datetime.date(2026, 11, 24)                  # countdown announced: progress bar starts here
MYT = datetime.timezone(datetime.timedelta(hours=8))  # count days in Malaysia time


def words(days):  # -> (big text, small label, subtitle, button, footer)
    if days > 0:
        return str(days), "DAY" if days == 1 else "DAYS", "releases on %s" % RELEASE.strftime("%d %B %Y").lstrip("0"), \
            "Try the beta now →", "Beta is being tested on real SmartHubs"
    return "V2.0", "IS OUT", "released %s" % RELEASE.strftime("%d %B %Y").lstrip("0"), "Get it →", "Now the stable version"


def svg(today):
    days = (RELEASE - today).days
    big, label, sub, btn, foot = words(days)
    done = min(1, max(0, (today - START).days / (RELEASE - START).days))
    size = 52 if len(big) <= 2 else 40 if len(big) == 3 else 32
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 150" font-family="Segoe UI,Helvetica,Arial,sans-serif">
  <rect x="2" y="2" width="796" height="146" rx="22" fill="#EAF5FC" stroke="#cfe6f5" stroke-width="3"/>
  <g transform="translate(34 42) scale(.5)">
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
  <rect x="190" y="22" width="112" height="106" rx="16" fill="#1d2330"/>
  <text x="246" y="{70 + size * .35:.0f}" font-size="{size}" font-weight="800" fill="#fff" text-anchor="middle">{big}</text>
  <text x="246" y="116" font-size="13" font-weight="800" fill="#ffd23f" text-anchor="middle" letter-spacing="2">{label}</text>
  <text x="326" y="58" font-size="22" font-weight="800" fill="#1d2330">SmartHub Dashboard V2.0</text>
  <text x="326" y="83" font-size="15" fill="#6b7684">{sub}</text>
  <rect x="326" y="98" width="300" height="12" rx="6" fill="#d5dbe2"/>
  <rect x="326" y="98" width="{300 * done:.0f}" height="12" rx="6" fill="#1E9AD6"/>
  <text x="326" y="130" font-size="12" fill="#8d99a8">{foot}</text>
  <rect x="632" y="60" width="150" height="32" rx="16" fill="#1E9AD6" stroke="#1d2330" stroke-width="2"/>
  <text x="707" y="81" font-size="13" font-weight="800" fill="#fff" text-anchor="middle">{btn}</text>
</svg>
"""


def selftest():
    assert words(21)[:2] == ("21", "DAYS") and words(1)[:2] == ("1", "DAY")
    assert words(0)[:2] == ("V2.0", "IS OUT") and words(-5)[:2] == ("V2.0", "IS OUT")
    assert words(3)[2] == "releases on 15 December 2026"
    assert 'width="0"' in svg(START) and 'width="300"' in svg(RELEASE + datetime.timedelta(days=9))


if __name__ == "__main__":
    selftest()
    open(sys.argv[1], "w", encoding="utf-8").write(svg(datetime.datetime.now(MYT).date()))
