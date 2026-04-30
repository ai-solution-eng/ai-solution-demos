def compute_tangent(pts, i):
    """
    Compute the vector tangent to pts line in i-th point, using finite diffence method.

    Args:
        pts: array [x, y, z] coordinates of centerline
        i: int index of the coordinates

    Returns:
        Tangent vector
    """

    if i == 0:
        return pts[i + 1] - pts[i]
    elif i == len(pts) - 1:
        return pts[i] - pts[i - 1]
    else:
        return pts[i + 1] - pts[i - 1]
