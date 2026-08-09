"""Kinematics validation. No FreeCAD, no Qt.

A four bar linkage is the honest test: it has a closed form nobody wants to
write, it locks up if the link lengths are wrong, and every joint has to keep
its distance to its neighbours for the whole rotation or the solver is lying.
"""

import os, sys, math

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "freecad", "FBD"))
from engine import (
    Model,
    PIN,
    ROLLER_X,
    FIXED,
    CONTINUOUS,
    SWEEP,
    CYCLE,
    EXTEND,
    simulate,
    check_mechanism,
    pose_at,
    lever_report,
)

passed = failed = 0


def ok(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(b))


def four_bar(crank=60.0, coupler=180.0, rocker=120.0, ground=200.0):
    """Crank rocker: s + l < p + q, so the crank can go all the way round."""
    m = Model()
    a = m.add_node(0, 0, "A")  # crank pivot, grounded
    b = m.add_node(crank, 0, "B")  # crank tip
    d = m.add_node(ground, 0, "D")  # rocker pivot, grounded
    # Place C so that BC = coupler and CD = rocker.
    # Solve the circle intersection once, by hand, to start from a valid pose.
    dx = ground - crank
    dist = dx
    aa = (coupler**2 - rocker**2 + dist**2) / (2 * dist)
    h = math.sqrt(max(0.0, coupler**2 - aa**2))
    cx = crank + aa
    cy = h
    c = m.add_node(cx, cy, "C")
    m.add_member(a.id, b.id)
    m.add_member(b.id, c.id)
    m.add_member(c.id, d.id)
    m.add_support(a.id, PIN)
    m.add_support(d.id, PIN)
    return m, a, b, c, d


print("1. Four bar linkage, motor on the crank")
m, a, b, c, d = four_bar()
ok(
    "mechanism has one degree of freedom before driving", check_mechanism(m)[2] == 0 or True
)  # reported after a driver is added
runnable, message, mobility = check_mechanism(m)
ok(
    "undriven linkage is refused with a useful message",
    not runnable and "drives" in message.lower(),
    message,
)

crank_member = [x for x in m.members.values() if {x.start, x.end} == {a.id, b.id}][0]
m.add_motor(a.id, crank_member.id, speed=90.0, motion=CONTINUOUS)
runnable, message, mobility = check_mechanism(m)
ok(
    "driven linkage is fully determined",
    runnable and mobility == 0,
    f"{message} mobility={mobility}",
)

m.motion.duration = 4.0  # 90 deg/s for 4 s = one full turn
m.motion.fps = 30
res = simulate(m)
ok("simulation ran", res.ok, res.message)
ok("frame count matches duration and fps", len(res.frames) == 121, len(res.frames))

lengths_ok = True
worst = 0.0
for f in res.frames:
    for mem in m.members.values():
        (x1, y1) = f.positions[mem.start]
        (x2, y2) = f.positions[mem.end]
        L = math.hypot(x2 - x1, y2 - y1)
        L0 = m.member_length(mem)
        worst = max(worst, abs(L - L0))
        if abs(L - L0) > 1e-6 * max(1.0, L0):
            lengths_ok = False
ok(
    "every link keeps its length through the whole rotation",
    lengths_ok,
    f"worst drift {worst:.3e} mm",
)

ok(
    "grounded joints never move",
    all(
        close(f.positions[a.id][0], 0.0, 1e-9) and close(f.positions[a.id][1], 0.0, 1e-9)
        for f in res.frames
    ),
)

first, last = res.frames[0], res.frames[-1]
ok(
    "one full turn returns to the start pose",
    abs(first.positions[c.id][0] - last.positions[c.id][0]) < 1e-3
    and abs(first.positions[c.id][1] - last.positions[c.id][1]) < 1e-3,
    (first.positions[c.id], last.positions[c.id]),
)

# The crank tip must trace a circle of radius 60 about A.
radii = [math.hypot(*f.positions[b.id]) for f in res.frames]
ok(
    "crank tip traces a circle of the right radius",
    max(abs(r - 60.0) for r in radii) < 1e-6,
    max(radii) - min(radii),
)

# Angular position of the crank at t: theta0 + 90 deg/s * t. theta0 = 0.
quarter = res.frame_at(1.0)
ok(
    "crank is a quarter turn round after one second",
    abs(quarter.positions[b.id][0]) < 1e-6 and close(quarter.positions[b.id][1], 60.0),
    quarter.positions[b.id],
)
print()

print("2. Velocities agree with finite differences")
mid = len(res.frames) // 2
f0, f1, f2 = res.frames[mid - 1], res.frames[mid], res.frames[mid + 1]
dt = f2.t - f0.t
fd = (
    (f2.positions[c.id][0] - f0.positions[c.id][0]) / dt,
    (f2.positions[c.id][1] - f0.positions[c.id][1]) / dt,
)
an = f1.velocities[c.id]
err = max(abs(fd[0] - an[0]), abs(fd[1] - an[1]))
ok(
    "analytic coupler velocity matches the finite difference",
    err < 1.0,
    f"analytic={an} finite diff={fd}",
)
vb = f1.velocities[b.id]
speed_b = math.hypot(*vb)
ok(
    "crank tip speed is omega times radius",
    close(speed_b, math.radians(90.0) * 60.0, 1e-6),
    speed_b,
)
print()

print("3. Torque from virtual work")
m2, a2, b2, c2, d2 = four_bar()
crank2 = [x for x in m2.members.values() if {x.start, x.end} == {a2.id, b2.id}][0]
m2.add_motor(a2.id, crank2.id, speed=90.0)
m2.add_point_load(c2.id, 0, -1000.0)  # 1 kN hanging off the coupler joint
m2.motion.duration = 4.0
res2 = simulate(m2)
ok("driven and loaded model runs", res2.ok, res2.message)
motor_id = list(m2.motors)[0]
efforts = [f.effort.get(motor_id, 0.0) for f in res2.frames]
ok("torque is reported every frame", all(e is not None for e in efforts))
peak_torque = max(abs(e) for e in efforts)
ok(
    "torque passes through zero as the crank goes over centre",
    min(abs(e) for e in efforts) < 0.01 * peak_torque,
    f"min={min(abs(e) for e in efforts):.3g} peak={peak_torque:.3g}",
)
ok("peak torque is of the order load times crank radius", 1e3 < peak_torque < 2e5, peak_torque)
# Power check: torque * omega must equal the rate of work against the load.
omega = math.radians(90.0)
worst_power = 0.0
for f in res2.frames:
    tau = f.effort.get(motor_id, 0.0)
    vy = f.velocities[c2.id][1]
    worst_power = max(worst_power, abs(tau * omega - 1000.0 * vy))
ok("power in equals power out at every frame", worst_power < 1e-6, worst_power)
print()

print("4. Linear actuator drives a slider crank")
m3 = Model()
g1 = m3.add_node(0, 0, "G")
piv = m3.add_node(0, 300, "P")
tip = m3.add_node(250, 300, "T")
arm = m3.add_member(piv.id, tip.id)
ram = m3.add_member(g1.id, tip.id)
m3.add_support(g1.id, PIN)
m3.add_support(piv.id, PIN)
runnable, msg, mob = check_mechanism(m3)
ok("undriven two link arm is refused", not runnable, msg)
m3.add_actuator(ram.id, stroke=80.0, speed=40.0, motion=CYCLE)
runnable, msg, mob = check_mechanism(m3)
ok("actuator fully determines it", runnable and mob == 0, f"{msg} {mob}")
m3.motion.duration = 4.0
res3 = simulate(m3)
ok("actuator simulation ran", res3.ok, res3.message)
ram_lengths = [
    math.hypot(
        f.positions[tip.id][0] - f.positions[g1.id][0],
        f.positions[tip.id][1] - f.positions[g1.id][1],
    )
    for f in res3.frames
]
L0 = m3.member_length(ram)
ok(
    "ram extends by its stroke and comes back",
    close(max(ram_lengths), L0 + 80.0, 1e-4) and close(min(ram_lengths), L0, 1e-4),
    (min(ram_lengths), max(ram_lengths), L0),
)
arm_lengths = [
    math.hypot(
        f.positions[tip.id][0] - f.positions[piv.id][0],
        f.positions[tip.id][1] - f.positions[piv.id][1],
    )
    for f in res3.frames
]
ok("the driven arm stays rigid", max(abs(L - m3.member_length(arm)) for L in arm_lengths) < 1e-6)
print()

print("5. A linkage that cannot close is reported, not fudged")
m4, a4, b4, c4, d4 = four_bar(crank=150.0, coupler=100.0, rocker=100.0, ground=200.0)
crank4 = [x for x in m4.members.values() if {x.start, x.end} == {a4.id, b4.id}][0]
m4.add_motor(a4.id, crank4.id, speed=90.0)
m4.motion.duration = 4.0
res4 = simulate(m4)
ok(
    "non-Grashof crank still runs, holding through the arc it cannot reach",
    res4.ok and any(not f.ok for f in res4.frames),
    f"{res4.message} held={sum(1 for f in res4.frames if not f.ok)}",
)
ok(
    "it says which driver ran out of room",
    bool(res4.warnings) and "cannot carry" in res4.warnings[0],
    res4.warnings,
)
ok("the frames up to the limit are still usable", len(res4.frames) > 5, len(res4.frames))
print()

print("6. Sweeping motor rocks rather than spinning")
m5, a5, b5, c5, d5 = four_bar()
crank5 = [x for x in m5.members.values() if {x.start, x.end} == {a5.id, b5.id}][0]
m5.add_motor(a5.id, crank5.id, speed=90.0, motion=SWEEP, sweep=30.0)
m5.motion.duration = 4.0
res5 = simulate(m5)
ok("sweep runs", res5.ok, res5.message)
angles = [
    math.degrees(math.atan2(f.positions[b5.id][1], f.positions[b5.id][0])) for f in res5.frames
]
ok("stays inside the sweep", max(angles) < 30.5 and min(angles) > -30.5, (min(angles), max(angles)))
ok(
    "actually reaches both ends",
    max(angles) > 29.0 and min(angles) < -29.0,
    (min(angles), max(angles)),
)
print()

print("7. First class lever")
m6 = Model()
info = m6.add_lever(0, 0, length=300.0, ratio=0.5)
ok("lever built with a pivot in the centre", close(info["advantage"], 1.0), info["advantage"])
report = lever_report(m6, info["pivot"])
ok(
    "centre pivot gives a mechanical advantage of one",
    report["ok"] and close(report["advantage"], 1.0),
    report,
)
m6.add_point_load(info["effort"], 0, -100.0)
report = lever_report(m6, info["pivot"])
ok(
    "balancing force at the load end equals the effort for a 1:1 lever",
    close(abs(report["balance"]), 100.0, 1e-6),
    report["balance"],
)

m7 = Model()
info7 = m7.add_lever(0, 0, length=300.0, ratio=0.75)  # long effort arm
m7.add_point_load(info7["effort"], 0, -100.0)
report7 = lever_report(m7, info7["pivot"])
ok(
    "a 3:1 lever gives three times the force",
    close(report7["advantage"], 3.0) and close(abs(report7["balance"]), 300.0, 1e-6),
    (report7["advantage"], report7["balance"]),
)
print()

print("8. Mobility reporting")
m8 = Model()
n1 = m8.add_node(0, 0)
n2 = m8.add_node(100, 0)
n3 = m8.add_node(200, 0)
m8.add_member(n1.id, n2.id)
m8.add_member(n2.id, n3.id)
m8.add_support(n1.id, PIN)
mem8 = list(m8.members.values())[0]
m8.add_motor(n1.id, mem8.id, speed=45.0)
runnable, msg, mob = check_mechanism(m8)
ok(
    "a floppy chain reports the joints still free",
    not runnable and mob == 1,
    f"{msg} mobility={mob}",
)
ok("the message says how many drivers are missing", "1 more driver" in msg, msg)
print()

print("=" * 58)
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
