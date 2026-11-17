# -*- coding: utf-8 -*-
"""Small music engraver for melodies stored as JSON in Anki notes (field "MelodyJSON").

Some Anki decks - sight singing, rhythm and interval drills - keep the melody as data and let a
script in the card template draw the staff while reviewing. The KnowledgeBase Builder runs no
template scripts, so the importer draws the staff itself: melody_svg() returns an SVG image
(treble or bass clef, key signature, time signature, notes with stems, flags and beams, dots,
ledger lines, accidentals, rests, ties and triplets). solfege() and rhythm_count() give the
answers: the syllables in movable do (minor based on la, raised seventh "si") and the count.

Melody format (version 2):

    {"clef": "treble", "key": "C", "mode": "major", "keySig": "C", "timeSig": "4/4",
     "keyAccidentals": {"F": 1, ...},
     "bars": [{"events": [{"kind": "note", "pitch": "E4", "duration": "q"},
                          {"kind": "rest", "duration": "8"},
                          {"kind": "note", "pitch": "G#4", "duration": "8", "tie": true, "tuplet": true}]}]}

Durations: w, h, q, 8 (eighth), qd (dotted quarter), hd, 16; "tuplet": true = triplet.
Standard library only.
"""
import base64
import json
import re

SPACE = 10                      # distance of the staff lines
STAFF_TOP = 40                  # y of the top line
STAFF_BOTTOM = STAFF_TOP + 4 * SPACE
HEIGHT = 118
INK = "#1A1A1A"

LETTERS = "CDEFGAB"
SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
QUARTERS = {"w": 4.0, "hd": 3.0, "h": 2.0, "qd": 1.5, "q": 1.0, "8d": 0.75, "8": 0.5, "16": 0.25}
# diatonic index (octave * 7 + letter) of the bottom staff line
BOTTOM_LINE = {"treble": 4 * 7 + 2, "bass": 2 * 7 + 4}      # E4, G2
# key signatures: staff positions (0 = bottom line, 1 step = half a space) of the accidentals
SHARP_POSITIONS = {"treble": [8, 5, 9, 6, 3, 7, 4], "bass": [6, 3, 7, 4, 1, 5, 2]}
FLAT_POSITIONS = {"treble": [4, 7, 3, 6, 2, 5, 1], "bass": [2, 5, 1, 4, 0, 3, -1]}
SHARP_ORDER = "FCGDAEB"
FLAT_ORDER = "BEADGCF"
SYLLABLES = ["do", "di", "re", "ri", "mi", "fa", "fi", "so", "si", "la", "te", "ti"]
MINOR_MODES = ("minor", "natural_minor", "harmonic_minor", "melodic_minor", "aeolian")


def parse_melody(text):
    """MelodyJSON field -> dict, or None when the field holds no melody."""
    text = re.sub(r"<[^>]+>", "", text or "").strip()
    text = text.replace("&quot;", '"').replace("&amp;", "&").replace("&nbsp;", " ")
    if not text.startswith("{"):
        return None
    try:
        melody = json.loads(text)
    except ValueError:
        return None
    return melody if isinstance(melody, dict) and isinstance(melody.get("bars"), list) else None


def parse_pitch(pitch):
    """'G#4' -> (letter, alteration, octave) or None."""
    match = re.match(r"^([A-Ga-g])(##|#|bb|b|n)?(-?\d)$", str(pitch or ""))
    if not match:
        return None
    alteration = {"": 0, "#": 1, "##": 2, "b": -1, "bb": -2, "n": 0}[match.group(2) or ""]
    return match.group(1).upper(), alteration, int(match.group(3))


def event_quarters(event):
    value = QUARTERS.get(str(event.get("duration", "q")), 1.0)
    if str(event.get("duration", "")).endswith("t") or event.get("tuplet"):
        base = QUARTERS.get(str(event.get("duration", "q")).rstrip("t"), 0.5)
        value = base * 2.0 / 3.0
    return value


def time_signature(melody):
    match = re.match(r"^(\d+)/(\d+)$", str(melody.get("timeSig", "4/4")))
    return (int(match.group(1)), int(match.group(2))) if match else (4, 4)


def beat_length(melody):
    """Beat in quarters: 1 in simple time, a dotted quarter in compound time (6/8, 9/8, 12/8)."""
    upper, lower = time_signature(melody)
    if lower == 8 and upper % 3 == 0 and upper > 3:
        return 1.5
    return 4.0 / lower


def key_alterations(melody):
    accidentals = melody.get("keyAccidentals") or {}
    return {letter: int(accidentals.get(letter, 0) or 0) for letter in LETTERS}


def do_semitone(melody):
    """Pitch class of "do": the tonic in major, the relative major (three semitones up) in minor."""
    key = parse_pitch(str(melody.get("key", "C")) + "4")
    tonic = (SEMITONES[key[0]] + key[1]) % 12 if key else 0
    if str(melody.get("mode", "major")).lower() in MINOR_MODES:
        tonic = (tonic + 3) % 12
    return tonic


def events(melody):
    for bar in melody.get("bars", []):
        for event in bar.get("events", []):
            yield event


def solfege(melody):
    """Syllables of the notes in movable do, e.g. 'mi mi so mi'."""
    do = do_semitone(melody)
    result = []
    for event in events(melody):
        if event.get("kind") != "note":
            continue
        pitch = parse_pitch(event.get("pitch"))
        if pitch:
            result.append(SYLLABLES[(SEMITONES[pitch[0]] + pitch[1] - do) % 12])
    return " ".join(result)


def note_names(melody):
    names = []
    for event in events(melody):
        if event.get("kind") == "note" and event.get("pitch"):
            names.append(str(event["pitch"]).replace("#", "♯").replace("b", "♭") if len(str(event["pitch"])) > 2
                         else str(event["pitch"]))
    return " ".join(names)


def rhythm_count(melody):
    """Count of the rhythm, bar by bar: '1 2 3 –' (– = held), '(4)' = rest, '+' = and, 'trip let'
    for triplets. In compound time the eighths are counted 1 to 6."""
    upper, lower = time_signature(melody)
    compound = (lower == 8)
    unit = 0.5 if compound else 4.0 / lower
    bars = []
    held_by_tie = False
    for bar in melody.get("bars", []):
        words = []
        position = 0.0
        for event in bar.get("events", []):
            length = event_quarters(event)
            rest = event.get("kind") == "rest"
            count = position / unit
            beat = int(round(count, 6)) if abs(count - round(count)) < 0.01 else int(count)
            fraction = count - int(count)
            if abs(fraction) < 0.01 or abs(fraction - 1) < 0.01:
                label = str(int(round(count)) + 1)
            elif abs(fraction - 0.5) < 0.01:
                label = "+"
            elif abs(fraction - 1.0 / 3) < 0.05:
                label = "trip"
            elif abs(fraction - 2.0 / 3) < 0.05:
                label = "let"
            else:
                label = "e" if fraction < 0.5 else "a"
            if rest:
                label = "(%s)" % label
            elif held_by_tie:
                label = "–"
            words.append(label)
            # sustained counts of a long note
            covered = int(round(length / unit)) - 1 if abs(fraction) < 0.01 else 0
            words.extend(["(–)" if rest else "–"] * max(0, covered))
            held_by_tie = bool(event.get("tie")) and not rest
            position += length
            del beat
        bars.append(" ".join(words))
    return " | ".join(bars)


SUBDIVISION_LABELS = {1: [""], 2: ["", "+"], 3: ["", "trip", "let"], 4: ["", "e", "+", "a"]}


def rhythm_grid(melody):
    """Full counting grid, bar by bar: a list of bars, each a list of (label, state) with state
    "clap" (a note starts), "hold" (a note sounds on), "rest" (a rest starts) or "silent" (a rest goes on).
    Every beat is divided as finely as its notes need (eighths: 1 +, triplets: 1 trip let, sixteenths:
    1 e + a); in compound time the eighths are the counts (1 to 6)."""
    upper, lower = time_signature(melody)
    compound = (lower == 8)
    unit = 0.5 if compound else 4.0 / lower
    beats = upper if compound else int(round(upper * (4.0 / lower) / unit))
    result = []
    tied = False
    for bar in melody.get("bars", []):
        spans = []
        position = 0.0
        for event in bar.get("events", []):
            length = event_quarters(event)
            rest = event.get("kind") == "rest"
            spans.append((position, position + length, rest, (not rest) and not tied))
            tied = bool(event.get("tie")) and not rest
            position += length
        cells = []
        for beat in range(beats):
            start = beat * unit
            onsets = [s[0] - start for s in spans if start - 1e-6 <= s[0] < start + unit - 1e-6]
            division = next((k for k in (1, 2, 3, 4)
                             if all(abs(o / unit * k - round(o / unit * k)) < 0.02 for o in onsets)), 4)
            for slot in range(division):
                time = start + slot * unit / division
                label = str(beat + 1) if slot == 0 else SUBDIVISION_LABELS[division][slot]
                span = next((s for s in spans if s[0] - 1e-6 <= time < s[1] - 1e-6), None)
                if span is None:
                    state = "silent"
                elif abs(span[0] - time) < 1e-6:
                    state = "rest" if span[2] else ("clap" if span[3] else "hold")
                else:
                    state = "silent" if span[2] else "hold"
                cells.append((label, state))
        result.append(cells)
    return result


def rhythm_grid_html(melody):
    """The grid as HTML: claps bold, held counts grey, rests in brackets."""
    styles = {"clap": "font-weight:bold", "hold": "color:#9AA0A6", "rest": "color:#9AA0A6", "silent": "color:#9AA0A6"}
    bars = []
    for cells in rhythm_grid(melody):
        words = []
        for label, state in cells:
            text = "(%s)" % label if state in ("rest", "silent") else label
            words.append('<span style="%s">%s</span>' % (styles[state], text))
        bars.append("&nbsp;".join(words))
    return '&nbsp;<span style="color:#9AA0A6">|</span>&nbsp;'.join(bars)


def rhythm_grid_text(melody):
    """The grid as plain text: claps as they are, held counts with ~, rests in brackets."""
    bars = []
    for cells in rhythm_grid(melody):
        bars.append(" ".join("(%s)" % l if s in ("rest", "silent") else (l + "~" if s == "hold" else l) for l, s in cells))
    return " | ".join(bars)


# ------------------------------------------------------------------------------------------------
# Engraving
# ------------------------------------------------------------------------------------------------

def _y(position):
    return STAFF_BOTTOM - position * SPACE / 2.0


def _f(value):
    return ("%.1f" % value).rstrip("0").rstrip(".")


TREBLE_CLEF = ("M21.5 73.5 C16 73.5 14.5 66 19.5 63 C25.5 59.5 32.5 64 30.5 71.5 C28.5 79 17 81.5 11.5 75 "
               "C6 68 10.5 58.5 18 51 C24 45 29 38.5 28.5 29 C28 21 23.5 18 21.5 22.5 C19 28 18.5 38 20 47 "
               "L24.5 92 C25.5 99.5 19.5 103 15 99.5")
BASS_CLEF = "M9.5 50 C9 42.5 17 39.5 22.5 41.5 C29.5 44 30.5 53.5 26 61 C22 67.5 15 72.5 7.5 76"


def _clef(clef):
    if clef == "bass":
        return ('<path d="%s" fill="none" stroke="%s" stroke-width="2.6" stroke-linecap="round"/>'
                '<circle cx="11" cy="49.5" r="3.6" fill="%s"/>'
                '<circle cx="34" cy="45" r="1.8" fill="%s"/><circle cx="34" cy="55" r="1.8" fill="%s"/>'
                % (BASS_CLEF, INK, INK, INK, INK))
    return ('<path d="%s" fill="none" stroke="%s" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>'
            '<circle cx="16" cy="97" r="3.3" fill="%s"/>' % (TREBLE_CLEF, INK, INK))


def _sharp(x, y):
    return ('<g stroke="%s" stroke-linecap="butt"><line x1="%s" y1="%s" x2="%s" y2="%s" stroke-width="1.2"/>'
            '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke-width="1.2"/>'
            '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke-width="2.6"/>'
            '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke-width="2.6"/></g>' % (
                INK, _f(x + 2), _f(y - 9), _f(x + 2), _f(y + 10), _f(x + 6), _f(y - 10), _f(x + 6), _f(y + 9),
                _f(x), _f(y - 2), _f(x + 8), _f(y - 4.5), _f(x), _f(y + 4.5), _f(x + 8), _f(y + 2)))


def _flat(x, y):
    return ('<g fill="none" stroke="%s"><line x1="%s" y1="%s" x2="%s" y2="%s" stroke-width="1.3"/>'
            '<path d="M%s %s C%s %s %s %s %s %s" stroke-width="1.9"/></g>' % (
                INK, _f(x + 1), _f(y - 14), _f(x + 1), _f(y + 4),
                _f(x + 1), _f(y), _f(x + 6), _f(y - 6), _f(x + 9), _f(y - 1), _f(x + 1), _f(y + 4)))


def _natural(x, y):
    return ('<g stroke="%s"><line x1="%s" y1="%s" x2="%s" y2="%s" stroke-width="1.2"/>'
            '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke-width="1.2"/>'
            '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke-width="2.4"/>'
            '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke-width="2.4"/></g>' % (
                INK, _f(x + 1), _f(y - 10), _f(x + 1), _f(y + 5), _f(x + 7), _f(y - 5), _f(x + 7), _f(y + 10),
                _f(x + 1), _f(y - 1.5), _f(x + 7), _f(y - 3.5), _f(x + 1), _f(y + 3.5), _f(x + 7), _f(y + 1.5)))


ACCIDENTAL = {1: _sharp, -1: _flat, 0: _natural}


def _rest(x, kind, beamed_center=None):
    middle = STAFF_TOP + 2 * SPACE
    if kind in ("w",):
        return '<rect x="%s" y="%s" width="12" height="5" fill="%s"/>' % (_f(x - 6), _f(STAFF_TOP + SPACE), INK)
    if kind in ("h", "hd"):
        return '<rect x="%s" y="%s" width="12" height="5" fill="%s"/>' % (_f(x - 6), _f(middle - 5), INK)
    if kind in ("q", "qd"):
        return ('<path d="M%s %s l6 8 l-5 6 l6 8 c-6 -3 -9 1 -4 6 c-8 -4 -7 -10 0 -9 l-7 -8 l5 -6 z" fill="%s"/>'
                % (_f(x - 3), _f(middle - 16), INK))
    # eighth and shorter
    return ('<g fill="%s" stroke="%s"><circle cx="%s" cy="%s" r="2.6" stroke="none"/>'
            '<path d="M%s %s C%s %s %s %s %s %s L%s %s" fill="none" stroke-width="1.6"/></g>' % (
                INK, INK, _f(x - 3), _f(middle - 4),
                _f(x - 3), _f(middle - 4), _f(x), _f(middle - 1), _f(x + 3), _f(middle - 3), _f(x + 4), _f(middle - 6),
                _f(x - 1), _f(middle + 12)))


def melody_svg(melody):
    """SVG of the melody as a string (width and height in pixels)."""
    clef = "bass" if str(melody.get("clef", "treble")).lower().startswith("bass") else "treble"
    bottom = BOTTOM_LINE[clef]
    alterations = key_alterations(melody)
    upper, lower = time_signature(melody)
    beat = beat_length(melody)
    parts = []
    x = 42.0                                   # after the clef

    # key signature
    sharps = [l for l in SHARP_ORDER if alterations[l] > 0]
    flats = [l for l in FLAT_ORDER if alterations[l] < 0]
    for i, letter in enumerate(sharps):
        parts.append(_sharp(x, _y(SHARP_POSITIONS[clef][SHARP_ORDER.index(letter)])))
        x += 10
    for i, letter in enumerate(flats):
        parts.append(_flat(x, _y(FLAT_POSITIONS[clef][FLAT_ORDER.index(letter)])))
        x += 9
    x += 4

    # time signature
    font = 'font-family="Georgia,\'Times New Roman\',serif" font-weight="bold" font-size="23" text-anchor="middle"'
    parts.append('<text x="%s" y="%s" %s fill="%s">%d</text>' % (_f(x + 8), _f(STAFF_TOP + 2 * SPACE - 2), font, INK, upper))
    parts.append('<text x="%s" y="%s" %s fill="%s">%d</text>' % (_f(x + 8), _f(STAFF_BOTTOM - 2), font, INK, lower))
    x += 30

    # layout: x and staff position of every event
    laid = []
    for bar_index, bar in enumerate(melody.get("bars", [])):
        bar_alterations = dict(alterations)
        position = 0.0
        for event in bar.get("events", []):
            length = event_quarters(event)
            item = {"event": event, "length": length, "onset": position, "bar": bar_index,
                    "rest": event.get("kind") == "rest", "accidental": None}
            if not item["rest"]:
                pitch = parse_pitch(event.get("pitch"))
                if not pitch:
                    item["rest"] = True
                else:
                    letter, alteration, octave = pitch
                    item["pos"] = octave * 7 + LETTERS.index(letter) - bottom
                    if bar_alterations.get(letter, 0) != alteration:
                        item["accidental"] = alteration
                        bar_alterations[letter] = alteration
                    x += 11 if item["accidental"] is not None else 0
            item["x"] = x + 6
            duration = str(event.get("duration", "q"))
            item["kind"] = duration.rstrip("t")
            x += 16 + 20 * (length ** 0.6)
            laid.append(item)
            position += length
        laid.append({"bar_line": True, "x": x})
        x += 10

    # beam groups: consecutive eighths (or shorter) within one beat
    groups = []
    current = []
    for item in laid:
        beamable = (not item.get("bar_line")) and (not item["rest"]) and item["length"] < 1.0 - 1e-6 \
            and item["kind"] in ("8", "16", "8d")
        if beamable and current and current[-1]["bar"] == item["bar"] \
                and int((current[-1]["onset"] + 1e-6) / beat) == int((item["onset"] + 1e-6) / beat):
            current.append(item)
            continue
        if len(current) > 1:
            groups.append(current)
        current = [item] if beamable else []
    if len(current) > 1:
        groups.append(current)
    in_group = {id(i): g for g in groups for i in g}

    # stem directions: per note, per beam group by the average
    for item in laid:
        if not item.get("bar_line") and not item["rest"]:
            item["up"] = item["pos"] < 4
    for group in groups:
        up = sum(i["pos"] for i in group) / float(len(group)) < 4
        for i in group:
            i["up"] = up

    staff_end = x + 6
    # staff lines
    for line in range(5):
        y = STAFF_TOP + line * SPACE
        parts.insert(0, '<line x1="4" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="1"/>' % (_f(y), _f(staff_end), _f(y), INK))
    parts.insert(5, _clef(clef))

    stem_length = 3.5 * SPACE
    for index, item in enumerate(laid):
        if item.get("bar_line"):
            last = index == len(laid) - 1
            parts.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="%s"/>' % (
                _f(item["x"]), _f(STAFF_TOP), _f(item["x"]), _f(STAFF_BOTTOM), INK, "1.2"))
            if last:
                parts.append('<rect x="%s" y="%s" width="4" height="%s" fill="%s"/>' % (
                    _f(item["x"] + 3), _f(STAFF_TOP), _f(4 * SPACE), INK))
            continue
        cx = item["x"]
        if item["rest"]:
            parts.append(_rest(cx, item["kind"]))
            if item["kind"] in ("qd", "hd", "8d"):
                parts.append('<circle cx="%s" cy="%s" r="1.8" fill="%s"/>' % (_f(cx + 9), _f(STAFF_TOP + 1.5 * SPACE), INK))
            continue
        pos = item["pos"]
        cy = _y(pos)
        # ledger lines
        for ledger in range(-2, pos - 1, -2):
            parts.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="1.1"/>' % (_f(cx - 9), _f(_y(ledger)), _f(cx + 9), _f(_y(ledger)), INK))
        for ledger in range(10, pos + 1, 2):
            parts.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="1.1"/>' % (_f(cx - 9), _f(_y(ledger)), _f(cx + 9), _f(_y(ledger)), INK))
        if item["accidental"] is not None:
            parts.append(ACCIDENTAL.get(max(-1, min(1, item["accidental"])), _natural)(cx - 20, cy))
        hollow = item["kind"] in ("w", "h", "hd")
        if hollow:
            parts.append('<ellipse cx="%s" cy="%s" rx="5.6" ry="3.6" transform="rotate(-20 %s %s)" fill="none" stroke="%s" stroke-width="1.8"/>' % (
                _f(cx), _f(cy), _f(cx), _f(cy), INK))
        else:
            parts.append('<ellipse cx="%s" cy="%s" rx="5.8" ry="4.1" transform="rotate(-20 %s %s)" fill="%s"/>' % (
                _f(cx), _f(cy), _f(cx), _f(cy), INK))
        if item["kind"] in ("qd", "hd", "8d"):
            dot_y = cy if pos % 2 else cy - SPACE / 2.0
            parts.append('<circle cx="%s" cy="%s" r="1.8" fill="%s"/>' % (_f(cx + 10), _f(dot_y), INK))
        if item["kind"] == "w":
            continue
        up = item["up"]
        stem_x = cx + 5.2 if up else cx - 5.2
        item["stem_x"] = stem_x
        group = in_group.get(id(item))
        if group is None:
            end_y = cy - stem_length if up else cy + stem_length
            if up and end_y > _y(4):
                end_y = _y(4)
            if not up and end_y < _y(4):
                end_y = _y(4)
            parts.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="1.3"/>' % (_f(stem_x), _f(cy), _f(stem_x), _f(end_y), INK))
            if item["kind"] in ("8", "16", "8d"):
                flags = 2 if item["kind"] == "16" else 1
                for f in range(flags):
                    offset = f * 7 * (1 if up else -1)
                    if up:
                        parts.append('<path d="M%s %s c0 6 10 9 9 18 c-0.5 3 -1.5 5 -2.5 6.5 c1.5 -6 -1 -11 -6.5 -13.5 z" fill="%s"/>' % (_f(stem_x - 0.6), _f(end_y + offset), INK))
                    else:
                        parts.append('<path d="M%s %s c0 -6 10 -9 9 -18 c-0.5 -3 -1.5 -5 -2.5 -6.5 c1.5 6 -1 11 -6.5 13.5 z" fill="%s"/>' % (_f(stem_x - 0.6), _f(end_y + offset), INK))

    # beams
    for group in groups:
        up = group[0]["up"]
        ys = [_y(i["pos"]) for i in group]
        beam_y = (min(ys) - stem_length) if up else (max(ys) + stem_length)
        beam_y = min(beam_y, _y(4)) if up else max(beam_y, _y(4))
        for i, y in zip(group, ys):
            parts.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="1.3"/>' % (_f(i["stem_x"]), _f(y), _f(i["stem_x"]), _f(beam_y), INK))
        first, last = group[0]["stem_x"], group[-1]["stem_x"]
        thickness = 4.5
        top = beam_y if up else beam_y - thickness
        parts.append('<rect x="%s" y="%s" width="%s" height="%s" fill="%s"/>' % (_f(first - 0.6), _f(top), _f(last - first + 1.2), _f(thickness), INK))
        if group[0]["kind"] == "16":
            second = top + (7 if up else -7)
            parts.append('<rect x="%s" y="%s" width="%s" height="%s" fill="%s"/>' % (_f(first - 0.6), _f(second), _f(last - first + 1.2), _f(thickness), INK))

    # triplets: "3" over (or under) each group of three tuplet events
    run = []
    for item in laid + [{"bar_line": True}]:
        if not item.get("bar_line") and item["event"].get("tuplet"):
            run.append(item)
            if len(run) < 3:
                continue
        if len(run) == 3:
            notes = [i for i in run if not i["rest"]]
            up = notes[0].get("up", True) if notes else True
            ys = [_y(i["pos"]) for i in notes] or [STAFF_TOP]
            y = (min(min(ys) - stem_length - 8, STAFF_TOP - 6)) if up else (max(max(ys) + stem_length + 18, STAFF_BOTTOM + 18))
            mid = (run[0]["x"] + run[-1]["x"]) / 2.0 + (2.6 if up else -2.6)
            parts.append('<text x="%s" y="%s" font-family="Georgia,serif" font-style="italic" font-size="14" text-anchor="middle" fill="%s">3</text>' % (_f(mid), _f(y), INK))
        run = [] if len(run) >= 3 or item.get("bar_line") or not item["event"].get("tuplet") else run

    # ties to the next note
    notes = [i for i in laid if not i.get("bar_line") and not i["rest"]]
    for current, following in zip(notes, notes[1:]):
        if current["event"].get("tie"):
            y = _y(current["pos"])
            below = current.get("up", True)
            offset = 6 if below else -6
            curve = 7 if below else -7
            x1, x2 = current["x"] + 3, following["x"] - 3
            parts.append('<path d="M%s %s Q%s %s %s %s" fill="none" stroke="%s" stroke-width="1.6"/>' % (
                _f(x1), _f(y + offset), _f((x1 + x2) / 2), _f(y + offset + curve), _f(x2), _f(y + offset), INK))

    width = int(staff_end + 8)
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'
            '<rect width="100%%" height="100%%" fill="#FFFFFF"/>%s</svg>' % (width, HEIGHT, width, HEIGHT, "".join(parts)))


def melody_img(melody, scale=1.0):
    """<img> with the SVG as a data URI."""
    svg = melody_svg(melody)
    width = int(re.search(r'width="(\d+)"', svg).group(1))
    data = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return '<img src="data:image/svg+xml;base64,%s" width="%d" height="%d" alt="">' % (
        data, int(width * scale), int(HEIGHT * scale))
