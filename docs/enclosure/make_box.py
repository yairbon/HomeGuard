"""HomeGuard door-board enclosure: writes tray.stl and lid.stl (PLA, print as they lie).

Run:  python make_box.py      (needs only numpy)
Every dimension is a parameter below, in millimetres. The solids are built on a 0.25 mm grid and
written as binary STL, so curved edges are stepped by 0.25 mm, finer than a 0.4 mm nozzle.

Layout (X = width 115, Y = height 90 when mounted, Z = depth):
  tray: back wall at z=0 (goes against the wall), four side walls, open at z=52.
  lid : flat plate, outer face at z=0. Print it outer-face-down.
  "top" side wall = +Y (vents), "bottom" side wall = -Y (cable and USB cutouts).
"""
import os
import numpy as np

RES = 0.25
W, H = 115.0, 90.0          # outer width (X) and height (Y)
TRAY_D = 52.0               # tray depth; tray + lid = 55 mm
WALL, BACK = 2.5, 3.0       # side wall and back wall thickness
LID_T = 3.0

# lid: keypad window and the four screws (screws go into the posts below)
KEYPAD_W, KEYPAD_H = 57.0, 46.5
SCREWS = [(6.0, 25.0), (6.0, 65.0), (H and W - 6.0, 25.0), (W - 6.0, 65.0)]
POST_R, POST_HOLE_R, POST_HOLE_DEPTH = 3.5, 1.3, 12.0   # M3 self-tapping
LID_HOLE_R, CSINK_R = 1.7, 3.25

# snap-fit version (no screws)
FIT = 0.3                      # gap between lip and wall
LIP_H, LIP_T = 6.0, 1.6        # lip height and thickness
SNAP_X0, SNAP_X1 = 30.0, 85.0  # bump / groove length on the long walls
SNAP_Z, SNAP_H, SNAP_DEPTH = TRAY_D - 2.5, 1.6, 0.6   # groove centre height in the tray, height, depth

# back wall: two diagonal mounting holes
MOUNT = [(10.0, 10.0), (W - 10.0, H - 10.0)]
MOUNT_R = 2.0

# zip-tie blocks for the ESP board (board lies with its USB end towards the bottom wall)
BLOCK_X, BLOCK_Y, BLOCK_Z = 12.0, 9.0, 5.0     # blocks sit either side of the board in Y
BLOCK_CENTER_OFFSET = 21.0          # from the middle, either side of the board
TIE_W, TIE_H, TIE_Z0 = 3.2, 1.8, 1.4   # tunnel width (X), height (Z), height above the back wall

# "bottom" = the short wall at x=0 (ESP's USB end), "top" = the short wall at x=W.
# Cutouts on the bottom wall: centres y from the bottom edge of the face, z from the outside of the back wall.
# The type-C pigtail opening = rectangle in the middle with a small screw hole either side of it.
USB = (H / 2, 12.0, 10.1, 12.4)          # centre y, centre z, width (Y), height (Z)
PAIR_Y, PAIR_Z, PAIR_SPACING, PAIR_R = H / 2, 12.0, 17.0, 2.5 / 2
CABLE_Y, CABLE_Z, CABLE_R = 76.0, 12.0, 5.9 / 2

# top wall (x=W) vents: 5 mm holes on a 7 mm grid, about half of the side
VENT_Y0, VENT_Z0, PITCH, VENT_R = 17.0, 9.0, 7.0, 2.5
VENT_COLS, VENT_ROWS = 9, 6


def grid(sx, sy, sz):
    nx, ny, nz = int(round(sx / RES)), int(round(sy / RES)), int(round(sz / RES))
    x = ((np.arange(nx) + 0.5) * RES)[:, None, None]
    y = ((np.arange(ny) + 0.5) * RES)[None, :, None]
    z = ((np.arange(nz) + 0.5) * RES)[None, None, :]
    return x, y, z, (nx, ny, nz)


def box(x, y, z, x0, x1, y0, y1, z0, z1):
    return (x >= x0) & (x < x1) & (y >= y0) & (y < y1) & (z >= z0) & (z < z1)


def cyl_z(x, y, z, cx, cy, r, z0, z1):
    return ((x - cx) ** 2 + (y - cy) ** 2 < r * r) & (z >= z0) & (z < z1)


def cyl_x(x, y, z, cy, cz, r, x0, x1):
    return ((y - cy) ** 2 + (z - cz) ** 2 < r * r) & (x >= x0) & (x < x1)


def tray(snap=False):
    x, y, z, shape = grid(W, H, TRAY_D)
    s = box(x, y, z, 0, W, 0, H, 0, TRAY_D) & ~box(x, y, z, WALL, W - WALL, WALL, H - WALL, BACK, TRAY_D + 1)
    if not snap:
        for cx, cy in SCREWS:                   # screw posts, 4 of them, up to the rim
            s |= cyl_z(x, y, z, cx, cy, POST_R, BACK - 0.5, TRAY_D)
            s &= ~cyl_z(x, y, z, cx, cy, POST_HOLE_R, TRAY_D - POST_HOLE_DEPTH, TRAY_D + 1)
    else:
        # snap version: a shallow groove on the inside of both long walls takes the lid's bumps,
        # and a finger notch in the rim of the -Y wall leaves the lid edge free to pry up
        for y0, y1 in ((WALL - SNAP_DEPTH, WALL + 0.1), (H - WALL - 0.1, H - WALL + SNAP_DEPTH)):
            s &= ~box(x, y, z, SNAP_X0, SNAP_X1, y0, y1, SNAP_Z - SNAP_H / 2, SNAP_Z + SNAP_H / 2)
        s &= ~box(x, y, z, W / 2 - 10, W / 2 + 10, -1, WALL - 0.8, TRAY_D - 3, TRAY_D + 1)
    for sgn in (-1, 1):                          # zip-tie blocks, one each side of the board
        cx, cy = W / 2, H / 2 + sgn * BLOCK_CENTER_OFFSET
        s |= box(x, y, z, cx - BLOCK_X / 2, cx + BLOCK_X / 2, cy - BLOCK_Y / 2, cy + BLOCK_Y / 2, BACK - 0.5, BACK + BLOCK_Z)
        # tunnel runs along Y, so a tie can pass over the board from one block to the other
        s &= ~box(x, y, z, cx - TIE_W / 2, cx + TIE_W / 2, cy - BLOCK_Y, cy + BLOCK_Y, BACK + TIE_Z0, BACK + TIE_Z0 + TIE_H)
    for mx, my in MOUNT:                         # wall screws through the back wall
        s &= ~cyl_z(x, y, z, mx, my, MOUNT_R, -1, BACK + 0.1)
    # bottom wall (x=0): rectangle in the middle, a small screw hole each side 17 mm apart, one 5.9 mm hole
    s &= ~box(x, y, z, -1, WALL + 0.1, USB[0] - USB[2] / 2, USB[0] + USB[2] / 2, USB[1] - USB[3] / 2, USB[1] + USB[3] / 2)
    for dy in (-PAIR_SPACING / 2, PAIR_SPACING / 2):
        s &= ~cyl_x(x, y, z, PAIR_Y + dy, PAIR_Z, PAIR_R, -1, WALL + 0.1)
    s &= ~cyl_x(x, y, z, CABLE_Y, CABLE_Z, CABLE_R, -1, WALL + 0.1)
    # top wall (x=W): vent holes
    for i in range(VENT_COLS):
        for j in range(VENT_ROWS):
            s &= ~cyl_x(x, y, z, VENT_Y0 + i * PITCH, VENT_Z0 + j * PITCH, VENT_R, W - WALL - 0.1, W + 1)
    return s


def lid(snap=False):
    x, y, z, shape = grid(W, H, LID_T)
    s = box(x, y, z, 0, W, 0, H, 0, LID_T)
    s &= ~box(x, y, z, (W - KEYPAD_W) / 2, (W + KEYPAD_W) / 2, (H - KEYPAD_H) / 2, (H + KEYPAD_H) / 2, -1, LID_T + 1)
    for cx, cy in SCREWS:
        s &= ~cyl_z(x, y, z, cx, cy, LID_HOLE_R, -1, LID_T + 1)
        r = CSINK_R - z * (CSINK_R - LID_HOLE_R) / 1.6      # countersink on the outer face
        s &= ~(((x - cx) ** 2 + (y - cy) ** 2 < r * r) & (z < 1.6))
    return s


def lid_snap():
    """Flat lid with a thin lip that plugs into the tray and two bumps that click into its grooves.
    No screws. The lip points to +Z (into the tray when the lid is turned over onto it)."""
    x, y, z, shape = grid(W, H, LID_T + LIP_H)
    s = box(x, y, z, 0, W, 0, H, 0, LID_T)
    s &= ~box(x, y, z, (W - KEYPAD_W) / 2, (W + KEYPAD_W) / 2, (H - KEYPAD_H) / 2, (H + KEYPAD_H) / 2, -1, LID_T + 1)
    lo, hi = WALL + FIT, H - WALL - FIT
    for ya, yb in ((lo, lo + LIP_T), (hi - LIP_T, hi)):           # long-side lips, flexible
        s |= box(x, y, z, 12, W - 12, ya, yb, LID_T - 0.1, LID_T + LIP_H)
    for xa, xb in ((WALL + FIT, WALL + FIT + LIP_T), (W - WALL - FIT - LIP_T, W - WALL - FIT)):  # short-side lips, locate only
        s |= box(x, y, z, xa, xb, 20, H - 20, LID_T - 0.1, LID_T + LIP_H)
    zc = LID_T + (TRAY_D - SNAP_Z)                                  # bump height in lid coordinates
    prof = np.clip(1 - np.abs(z - zc) / (SNAP_H / 2 + 0.2), 0, 1) * (SNAP_DEPTH + FIT - 0.1)
    inx = (x >= SNAP_X0 + 1) & (x < SNAP_X1 - 1)
    s |= inx & (y >= lo - prof) & (y < lo + 0.05) & (prof > 0.05)
    s |= inx & (y >= hi - 0.05) & (y < hi + prof) & (prof > 0.05)
    return s


def mesh(occ):
    """Triangles of the surface of a boolean grid, flat runs merged along one axis."""
    tris = []
    for ax in range(3):
        o, m = (ax + 1) % 3, (ax + 2) % 3
        t = np.transpose(occ, (ax, o, m))
        for sgn in (1, -1):
            nb = np.zeros_like(t)
            if sgn > 0:
                nb[:-1] = t[1:]
            else:
                nb[1:] = t[:-1]
            e = t & ~nb
            pad = np.zeros(e.shape[:2] + (1,), bool)
            ep = np.concatenate([pad, e, pad], axis=2)
            d = np.diff(ep.astype(np.int8), axis=2)
            si = np.argwhere(d == 1)
            ei = np.argwhere(d == -1)
            a = si[:, 0].astype(float) + (1 if sgn > 0 else 0)
            b0, b1 = si[:, 1].astype(float), si[:, 1].astype(float) + 1
            c0, c1 = si[:, 2].astype(float), ei[:, 2].astype(float)
            q = np.stack([[a, b0, c0], [a, b1, c0], [a, b1, c1], [a, b0, c1]], axis=0)  # (4, 3[ax,o,m], N)
            inv = np.argsort([ax, o, m])
            q = q[:, inv, :].transpose(2, 0, 1) * RES                                  # (N, 4, 3 xyz)
            n = np.cross(q[:, 1] - q[:, 0], q[:, 2] - q[:, 0])[:, ax] * sgn
            flip = n < 0
            q[flip] = q[flip][:, ::-1]
            tris.append(q[:, [0, 1, 2]])
            tris.append(q[:, [0, 2, 3]])
    return np.concatenate(tris).astype(np.float32)


def write_stl(path, tris):
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    rec = np.zeros(len(tris), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"], rec["v"] = n, tris
    with open(path, "wb") as f:
        f.write(b"HomeGuard door board enclosure".ljust(80, b" "))
        f.write(np.uint32(len(tris)).tobytes())
        f.write(rec.tobytes())


if __name__ == "__main__":
    out = os.path.dirname(os.path.abspath(__file__))
    for name, occ in (("tray", tray()), ("lid", lid()), ("tray_snap", tray(True)), ("lid_snap", lid_snap())):
        tris = mesh(occ)
        vol = np.einsum("ij,ij->", tris[:, 0].astype(float), np.cross(tris[:, 1].astype(float), tris[:, 2].astype(float))) / 6
        print(f"{name}: {len(tris)} triangles, mesh volume {vol / 1000:.2f} cm3, voxel volume {occ.sum() * RES ** 3 / 1000:.2f} cm3, "
              f"size {tris.reshape(-1, 3).max(0) - tris.reshape(-1, 3).min(0)}")
        write_stl(os.path.join(out, f"{name}.stl"), tris)
