"""The 80-space Boku board, in axial coordinates and normalized screen space."""
import math

AXES = ((1, 0), (0, 1), (1, -1))
DIRECTIONS = AXES + tuple((-q, -r) for q, r in AXES)
COORDINATES = {
    f'{chr(65+r)}{i+1}': (q, r)
    for r in range(11)
    for i, q in enumerate(q for q in range(10) if 5 <= q+r <= 14)
}
CELLS = tuple(COORDINATES)
AT = {coordinate: cell for cell, coordinate in COORDINATES.items()}
SPACING = .85 / 9
CENTERS = {cell: (.075 + (q+r/2-2.5)*SPACING,
                  .5 + (r-5)*math.sqrt(3)/2*SPACING)
           for cell, (q, r) in COORDINATES.items()}


def neighbor(cell, direction, distance=1):
    q, r = COORDINATES[cell]
    dq, dr = direction
    return AT.get((q+dq*distance, r+dr*distance))


def recognition_view(drawing):
    """Frame all pending ink plus neighbors, without inferring an action."""
    points = [p for stroke in drawing for p in stroke.points]
    if not points or any(len(p) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in p) for p in points):
        return 0., 0., 1.
    left, right = min(p[0] for p in points), max(p[0] for p in points)
    top, bottom = min(p[1] for p in points), max(p[1] for p in points)
    size = min(1., max(.32, right-left+2*SPACING, bottom-top+2*SPACING))
    return (max(0., min(1-size, (left+right-size)/2)),
            max(0., min(1-size, (top+bottom-size)/2)), size)


def geometry_matches(drawing, cell):
    """Validate location independently of the model's symbol classification."""
    cx, cy = CENTERS[cell]
    total = inside = 0.
    for stroke in drawing:
        if not stroke.points or any(len(p) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in p) for p in stroke.points):
            return False
        for (ax, ay), (bx, by) in zip(stroke.points, stroke.points[1:]):
            length = math.hypot(bx-ax, by-ay)
            steps = max(1, math.ceil(length/.001))
            for step in range(steps):
                t = (step+.5)/steps
                total += length/steps
                if math.hypot(ax+t*(bx-ax)-cx, ay+t*(by-ay)-cy) <= .48*SPACING:
                    inside += length/steps
    return total > 0 and inside/total >= .95


def pocket_edges(centers, radius):
    edges = []
    for x, y in centers.values():
        points = [(x+radius*math.cos(math.pi/6+i*math.pi/3),
                   y+radius*math.sin(math.pi/6+i*math.pi/3)) for i in range(6)]
        edges.extend((*points[i], *points[(i+1) % 6]) for i in range(6))
    return tuple(edges)
