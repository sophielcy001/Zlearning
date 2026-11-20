from collections import defaultdict
from hashlib import blake2b
from itertools import count


CELL_WIDTH = 136
CELL_HEIGHT = 108


def perimeter(radius: int):
    for x in range(-radius, radius):
        yield x, -radius
        yield -x, radius
    for y in range(-radius, radius):
        yield radius, y
        yield -radius, -y


def cells():
    yield 0, 0
    for radius in count(1):
        yield from perimeter(radius)


def cell(position: list[int]) -> tuple[int, int]:
    return round(position[0] / CELL_WIDTH), round(position[1] / CELL_HEIGHT)


def vacant_near(origin: tuple[int, int], occupied: set[tuple[int, int]], seed: int):
    for radius in range(1, 5):
        offsets = list(perimeter(radius))
        start = seed % len(offsets)
        for dx, dy in offsets[start:] + offsets[:start]:
            candidate = origin[0] + dx, origin[1] + dy
            if candidate not in occupied:
                return candidate
    return None


def add_anchors(layout: dict, nodes: dict[str, dict], edges: dict[str, str]) -> dict:
    missing = nodes.keys() - layout.keys()
    if not missing:
        return layout
    next_layout = dict(layout)
    occupied = {cell(position) for position in layout.values()}
    project_anchors = {}
    for identity, node in nodes.items():
        if identity in layout and node["kind"] == "course":
            project_anchors.setdefault(node["projectIds"][0], layout[identity])
    neighbors = defaultdict(list)
    for key in edges:
        a, b = key.split("|")
        neighbors[a].append(b)
        neighbors[b].append(a)
    unassigned = cells()
    for identity in sorted(missing, key=lambda item: (nodes[item]["kind"] != "course", item)):
        node = nodes[identity]
        seed = blake2b(identity.encode(), digest_size=4).digest()
        project = next(iter(node["projectIds"]), None)
        neighbor = next((next_layout[peer] for peer in sorted(neighbors[identity]) if peer in next_layout), None)
        near = neighbor if neighbor is not None else project_anchors.get(project)
        location = vacant_near(cell(near), occupied, seed[0]) if near is not None else None
        if location is None:
            location = next(candidate for candidate in unassigned if candidate not in occupied)
        x = location[0] * CELL_WIDTH + (seed[1] % 43 - 21)
        y = location[1] * CELL_HEIGHT + (seed[2] % 37 - 18)
        next_layout[identity] = [x, y]
        occupied.add(location)
        if node["kind"] == "course":
            project_anchors.setdefault(project, next_layout[identity])
    return next_layout
