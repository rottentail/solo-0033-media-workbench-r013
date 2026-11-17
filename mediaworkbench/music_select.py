"""Duration-exact global search over bounded phrase uses and directed joins.

A locally pleasant next phrase can spend the only remaining use that can close
the requested duration, so the whole route is optimized at once.  A search
state is the complete per-phrase usage vector plus the last phrase.  For a
fixed usage vector the elapsed programme time and the number of uses are both
fixed, so merging equal vectors loses nothing and the best score for each
state is exact.  Each state keeps the best path for its
(score, use count, catalogue-index sequence) order, which makes the documented
tie-breaking stable across processes.
"""

MAX_USES = 12


def _counts(code, count):
    return [(code >> (2 * index)) & 3 for index in range(count)]


def _elapsed(counts, durations, overlap):
    total = sum(count * duration for count, duration in zip(counts, durations))
    return total - overlap * (sum(counts) - 1)


def _capacity(counts, limits, durations, overlap):
    """Optimistic programme time still available from a usage vector."""
    capacity = 0
    for count, limit, duration in zip(counts, limits, durations):
        spare = limit - count
        if spare > 0:
            capacity += spare * (duration - overlap)
    return capacity


def choose(sections, joins, target_ms, overlap_ms, starts, ends):
    count = len(sections)
    durations = [section['duration_ms'] for section in sections]
    limits = [section['max_uses'] for section in sections]
    scores = [section['score'] for section in sections]
    names = [section['name'] for section in sections]
    closing = {index for index, section in enumerate(sections) if section['name'] in ends}
    steps = [duration - overlap_ms for duration in durations]

    # step[i][j] is the score change of appending j after i: score(j) - cost(i,j).
    step = [[None] * count for _ in range(count)]
    for edge in joins:
        source = names.index(edge['from'])
        target = names.index(edge['to'])
        step[source][target] = scores[target] - edge['cost']

    # Value is (-score, path) so a plain tuple comparison implements the
    # documented preference: higher score, then fewer uses, then the
    # lexicographically earliest catalogue-index sequence.
    states = {}
    for index, section in enumerate(sections):
        if section['name'] not in starts or durations[index] > target_ms:
            continue
        states[((1 << (2 * index)) << 3) | index] = (-scores[index], bytes([index]))

    best = None
    for uses in range(1, MAX_USES + 1):
        following = {}
        while states:
            key, (rank, path) = states.popitem()
            last = key & 7
            code = key >> 3
            counts = _counts(code, count)
            elapsed = _elapsed(counts, durations, overlap_ms)
            if elapsed == target_ms and last in closing:
                candidate = (rank, uses, path)
                if best is None or candidate < best:
                    best = candidate
            if uses == MAX_USES:
                continue
            # A prefix that cannot even reach the exact target within its
            # remaining use budget can never complete a route.
            if elapsed + _capacity(counts, limits, durations, overlap_ms) < target_ms:
                continue
            for index in range(count):
                change = step[last][index]
                if change is None or counts[index] >= limits[index]:
                    continue
                # A phrase that lands exactly on the target stays eligible so a
                # later closing phrase can still be appended; each step must
                # advance programme time (catalogue validation guarantees it).
                if steps[index] <= 0 or elapsed + steps[index] > target_ms:
                    continue
                next_code = code + (1 << (2 * index))
                next_key = (next_code << 3) | index
                next_value = (rank - change, path + bytes([index]))
                previous = following.get(next_key)
                if previous is None or next_value < previous:
                    following[next_key] = next_value
        states = following
        if not states:
            break

    if best is None:
        raise ValueError('no complete musical sequence fits the requested duration')
    _, _, path = best
    cursor = 0
    sequence = []
    for index in path:
        phrase = dict(sections[index])
        phrase['at_ms'] = cursor
        sequence.append(phrase)
        cursor += phrase['duration_ms'] - overlap_ms
    return dict(sequence=sequence, score=-best[0], duration_ms=target_ms,
                overlap_ms=overlap_ms)
