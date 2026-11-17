"""Duration-aware preview assembly using directed musical continuations."""

def _options(sections, costs, last, used, remaining, overlap):
    ranked = []
    for index, section in enumerate(sections):
        edge = (last,section['name'])
        if edge not in costs or used[index]>=section['max_uses']:
            continue
        duration = section['duration_ms']-overlap
        if duration>remaining:
            continue
        contribution = section['score']-costs[edge]
        ranked.append((-contribution,index,duration))
    ranked.sort()
    return ranked


def _assemble(sections,costs,opening,target,overlap,ends):
    used = [0]*len(sections)
    used[opening] = 1
    path = [opening]
    elapsed = sections[opening]['duration_ms']
    score = sections[opening]['score']
    while elapsed<target and len(path)<12:
        last = sections[path[-1]]['name']
        ranked = _options(sections,costs,last,used,target-elapsed,overlap)
        if not ranked:
            return None
        negative,following,duration = ranked[0]
        used[following] += 1
        path.append(following)
        elapsed += duration
        score -= negative
    if elapsed!=target or sections[path[-1]]['name'] not in ends:
        return None
    return dict(path=path,score=score)


def choose(sections, joins, target_ms, overlap_ms, starts, ends):
    costs = {(j['from'],j['to']):j['cost'] for j in joins}
    candidates = []
    for opening, section in enumerate(sections):
        if section['name'] not in starts or section['duration_ms']>target_ms:
            continue
        result = _assemble(sections,costs,opening,target_ms,overlap_ms,ends)
        if result is not None:
            candidates.append(result)
    if not candidates:
        raise ValueError('no complete musical sequence fits the requested duration')
    selected = min(candidates,key=lambda r:(-r['score'],len(r['path']),r['path']))
    cursor = 0
    sequence = []
    for index in selected['path']:
        phrase = dict(sections[index])
        phrase['at_ms'] = cursor
        sequence.append(phrase)
        cursor += phrase['duration_ms']-overlap_ms
    return dict(sequence=sequence,score=selected['score'],duration_ms=target_ms,overlap_ms=overlap_ms)
