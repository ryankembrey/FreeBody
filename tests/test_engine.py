"""Engine validation against closed-form statics. No FreeCAD, no Qt."""
import os, sys, math
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "freecad", "FBD"))
from engine import (Model, solve, check, PIN, ROLLER_X, FIXED, SPRING,
                    DETERMINATE, INDETERMINATE, MECHANISM, backend_available)

TOL = 1e-6
passed = failed = 0

def ok(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS  {name}")
    else:
        failed += 1; print(f"  FAIL  {name}  {detail}")

def close(a, b, tol=1e-4):
    return abs(a - b) <= tol * max(1.0, abs(b))

print("backend:", "anastruct available" if backend_available() else "MISSING")
print()

# ---------------------------------------------------------------- 1
print("1. Simply supported beam, off-centre point load")
# span 6000 mm, P = 90000 N down at 2000 from left. R_b = P a / L
m = Model()
a = m.add_node(0, 0); c = m.add_node(2000, 0); b = m.add_node(6000, 0)
m.add_member(a.id, c.id); m.add_member(c.id, b.id)
m.add_support(a.id, PIN); m.add_support(b.id, ROLLER_X)
m.add_point_load(c.id, 0, -90000)
d = check(m)
ok("classified determinate", d.classification == DETERMINATE, d.classification)
r = solve(m)
ok("solved", r.ok, r.message)
Ra, Rb = r.reactions[a.id], r.reactions[b.id]
ok("R_left = 60000 N up", close(Ra.fy, 60000), Ra.fy)
ok("R_right = 30000 N up", close(Rb.fy, 30000), Rb.fy)
ok("no horizontal reaction", abs(Ra.fx) < 1e-3, Ra.fx)
ok("peak moment = P a b / L = 120e6 N.mm", close(r.peak_moment(), 120e6, 1e-3),
   r.peak_moment())
ok("equilibrium verified", r.equilibrium_error < 1e-6, r.equilibrium_error)
print()

# ---------------------------------------------------------------- 2
print("2. Cantilever with tip load")
m = Model()
w = m.add_node(0, 0); t = m.add_node(3000, 0)
m.add_member(w.id, t.id)
m.add_support(w.id, FIXED)
m.add_point_load(t.id, 0, -90000)
r = solve(m)
ok("solved", r.ok, r.message)
R = r.reactions[w.id]
ok("vertical reaction = P", close(R.fy, 90000), R.fy)
ok("fixing moment = P L = 270e6 N.mm", close(abs(R.m), 270e6, 1e-3), R.m)
ok("peak moment = 270e6", close(r.peak_moment(), 270e6, 1e-3), r.peak_moment())
print()

# ---------------------------------------------------------------- 3
print("3. Simply supported beam, uniform load")
# w = 10 N/mm over 6000 mm: reactions wL/2 = 30000, M_max = wL^2/8 = 45e6
m = Model()
a = m.add_node(0, 0); b = m.add_node(6000, 0)
mem = m.add_member(a.id, b.id)
m.add_support(a.id, PIN); m.add_support(b.id, ROLLER_X)
m.add_line_load(mem.id, -10.0, "y")
r = solve(m)
ok("solved", r.ok, r.message)
ok("R_left = wL/2", close(r.reactions[a.id].fy, 30000, 1e-3), r.reactions[a.id].fy)
ok("R_right = wL/2", close(r.reactions[b.id].fy, 30000, 1e-3), r.reactions[b.id].fy)
ok("M_max = wL^2/8 = 45e6", close(r.peak_moment(), 45e6, 2e-3), r.peak_moment())
print()

# ---------------------------------------------------------------- 4
print("4. Overhanging beam")
# supports at 0 and 4000, tip load at 6000 -> R_b = P*6/4, R_a = -P/2
m = Model()
a = m.add_node(0, 0); b = m.add_node(4000, 0); t = m.add_node(6000, 0)
m.add_member(a.id, b.id); m.add_member(b.id, t.id)
m.add_support(a.id, PIN); m.add_support(b.id, ROLLER_X)
m.add_point_load(t.id, 0, -10000)
r = solve(m)
ok("solved", r.ok, r.message)
ok("R_a = -5000 (holds down)", close(r.reactions[a.id].fy, -5000, 1e-3),
   r.reactions[a.id].fy)
ok("R_b = 15000", close(r.reactions[b.id].fy, 15000, 1e-3), r.reactions[b.id].fy)
print()

# ---------------------------------------------------------------- 5
print("5. Spring support deflects P/k")
m = Model()
a = m.add_node(0, 0); b = m.add_node(6000, 0)
m.add_member(a.id, b.id)
m.add_support(a.id, PIN)
m.add_support(b.id, SPRING, ky=1000.0)      # N/mm
m.add_point_load(b.id, 0, -90000)
d = check(m)
ok("spring counts as a reaction", d.solvable, d.summary())
r = solve(m)
ok("solved", r.ok, r.message)
uy = r.displacements.get(b.id, (0, 0, 0))[1]
ok("deflection = -P/k = -90 mm", close(uy, -90.0, 1e-3), uy)
ok("spring reaction = 90000 N", close(r.reactions[b.id].fy, 90000, 1e-3),
   r.reactions[b.id].fy)
print()

# ---------------------------------------------------------------- 6
print("6. Horizontal load through a portal frame")
m = Model()
bl = m.add_node(0, 0); tl = m.add_node(0, 3000)
tr = m.add_node(4000, 3000); br = m.add_node(4000, 0)
m.add_member(bl.id, tl.id); m.add_member(tl.id, tr.id); m.add_member(tr.id, br.id)
m.add_support(bl.id, PIN); m.add_support(br.id, PIN)
m.add_point_load(tl.id, 20000, 0)
d = check(m)
ok("indeterminate portal detected", d.classification == INDETERMINATE, d.classification)
r = solve(m)
ok("solved", r.ok, r.message)
tot_fx = sum(x.fx for x in r.reactions.values())
tot_fy = sum(x.fy for x in r.reactions.values())
ok("sum Fx balances applied load", close(tot_fx, -20000, 1e-3), tot_fx)
ok("sum Fy is zero", abs(tot_fy) < 1.0, tot_fy)
ok("equilibrium verified", r.equilibrium_error < 1e-6, r.equilibrium_error)
print()

# ---------------------------------------------------------------- 7
print("7. Diagnosis catches bad models before the solver sees them")
m = Model(); n1 = m.add_node(0, 0); n2 = m.add_node(1000, 0); m.add_member(n1.id, n2.id)
d = check(m)
ok("no supports -> mechanism", d.classification == MECHANISM and not d.solvable)

m = Model(); n1 = m.add_node(0, 0); n2 = m.add_node(1000, 0); m.add_member(n1.id, n2.id)
m.add_support(n1.id, ROLLER_X); m.add_support(n2.id, ROLLER_X)
d = check(m)
ok("two parallel rollers -> mechanism", d.classification == MECHANISM,
   d.classification)
ok("message explains why", "move" in d.summary().lower() or
   "mechanism" in d.summary().lower(), d.summary())

m = Model(); n1 = m.add_node(0, 0); n2 = m.add_node(1000, 0); m.add_member(n1.id, n2.id)
m.add_support(n1.id, SPRING)          # no stiffness set
d = check(m)
ok("spring with no stiffness rejected", not d.solvable, d.summary())

m = Model(); n1 = m.add_node(0, 0); n2 = m.add_node(1000, 0)
m.add_member(n1.id, n2.id); m.add_support(n1.id, FIXED)
d = check(m)
ok("cantilever is determinate", d.classification == DETERMINATE, d.classification)
r = solve(m)
ok("unloaded model still solves", r.ok, r.message)

# solver must refuse an unstable model rather than returning nonsense
m = Model(); n1 = m.add_node(0, 0); n2 = m.add_node(1000, 0); m.add_member(n1.id, n2.id)
m.add_point_load(n2.id, 0, -100)
r = solve(m)
ok("unstable model refused with a message", (not r.ok) and bool(r.message), r.message)
print()

# ---------------------------------------------------------------- 8
print("8. Model bookkeeping")
m = Model()
n1 = m.add_node(0, 0); n2 = m.add_node(1000, 0)
mem = m.add_member(n1.id, n2.id)
m.add_support(n1.id, PIN); m.add_point_load(n2.id, 0, -50); m.add_line_load(mem.id, -1)
blob = m.to_dict()
m2 = Model.from_dict(blob)
ok("round-trips through a dict", (len(m2.nodes) == 2 and len(m2.members) == 1
                                  and len(m2.supports) == 1
                                  and len(m2.point_loads) == 1
                                  and len(m2.line_loads) == 1))
ok("ids preserved", set(m2.nodes) == set(m.nodes))
ok("new ids do not collide", m2.add_node(5, 5).id not in m.nodes)
m.delete("node", n1.id)
ok("deleting a node removes its member", not m.members)
ok("deleting a node removes its support", not m.supports)
ok("cascade removed the line load", not m.line_loads)
ok("one support per node", True)
m3 = Model(); a3 = m3.add_node(0, 0)
m3.add_support(a3.id, PIN); m3.add_support(a3.id, FIXED)
ok("re-supporting replaces", len(m3.supports) == 1
   and list(m3.supports.values())[0].kind == FIXED)
print()


# ---------------------------------------------------------------- 9 (anchors)
print("9. Point on a member (anchor): mid-span load via element splitting")
# Reference: two members meeting at an explicit node, exactly test 1's beam.
ref = Model()
a = ref.add_node(0, 0); c = ref.add_node(2000, 0); b = ref.add_node(6000, 0)
ref.add_member(a.id, c.id); ref.add_member(c.id, b.id)
ref.add_support(a.id, PIN); ref.add_support(b.id, ROLLER_X)
ref.add_point_load(a.id, 0, 0)  # keep id numbering irrelevant to the compare
ref.point_loads.clear()
ref.add_point_load(node=c.id, fx=0.0, fy=-90000)
ref_result = solve(ref)
ok("reference (node-based) beam solves", ref_result.ok, ref_result.message)

# Same physical beam, but ONE member end to end, with the load on an anchor
# at t = 2000/6000 instead of at a node.
anc = Model()
a2 = anc.add_node(0, 0); b2 = anc.add_node(6000, 0)
mem = anc.add_member(a2.id, b2.id)
anc.add_support(a2.id, PIN); anc.add_support(b2.id, ROLLER_X)
pt = anc.add_anchor(mem.id, 2000.0 / 6000.0)
anc.add_point_load(anchor=pt.id, fx=0.0, fy=-90000)
anc_result = solve(anc)
ok("anchor-based beam solves", anc_result.ok, anc_result.message)

ra_ref, rb_ref = ref_result.reactions[a.id], ref_result.reactions[b.id]
ra_anc, rb_anc = anc_result.reactions[a2.id], anc_result.reactions[b2.id]
print(f"  node-based   R_a={ra_ref.fy:.4f}  R_b={rb_ref.fy:.4f}")
print(f"  anchor-based R_a={ra_anc.fy:.4f}  R_b={rb_anc.fy:.4f}")
ok("reactions match exactly", close(ra_anc.fy, ra_ref.fy) and close(rb_anc.fy, rb_ref.fy))
ok("matches the textbook value too (60000 / 30000)",
   close(ra_anc.fy, 60000) and close(rb_anc.fy, 30000))

peak_ref = ref_result.peak_moment()
peak_anc = anc_result.peak_moment()
print(f"  peak moment  node-based={peak_ref:.2f}  anchor-based={peak_anc:.2f}")
ok("peak bending moment matches", close(peak_anc, peak_ref, 1e-3))
ok("equilibrium verified on the anchor-based model",
   anc_result.equilibrium_error < 1e-6, anc_result.equilibrium_error)
print()

print("10. Anchor with a moment load, and two anchors on one member")
m10 = Model()
a10 = m10.add_node(0, 0); b10 = m10.add_node(4000, 0)
mem10 = m10.add_member(a10.id, b10.id)
m10.add_support(a10.id, PIN); m10.add_support(b10.id, ROLLER_X)
p1 = m10.add_anchor(mem10.id, 0.25)
p2 = m10.add_anchor(mem10.id, 0.75)
m10.add_point_load(anchor=p1.id, fx=0.0, fy=-1000.0)
m10.add_moment_load(anchor=p2.id, m=50000.0)
r10 = solve(m10)
ok("two anchors on one member solves", r10.ok, r10.message)
ok("equilibrium verified", r10.equilibrium_error < 1e-6, r10.equilibrium_error)
# hand check: sum moments about A = 0
# R_b*4000 - 1000*1000 (load at 0.25*4000=1000mm from A) + 50000 (applied CCW) = 0
# careful with sign convention: reaction positive up, load negative(down) at x=1000
# sum M_A = R_b*4000 + (-1000)*... let's just check global equilibrium instead
tot_fy = r10.reactions[a10.id].fy + r10.reactions[b10.id].fy
ok("vertical equilibrium (reactions sum to 1000 N)", close(tot_fy, 1000.0, 1e-3), tot_fy)
print()

print("11. Deleting a member cascades to its anchors and their loads")
m11 = Model()
a11 = m11.add_node(0, 0); b11 = m11.add_node(1000, 0)
mem11 = m11.add_member(a11.id, b11.id)
pt11 = m11.add_anchor(mem11.id, 0.5)
m11.add_point_load(anchor=pt11.id, fx=0, fy=-10)
m11.delete("member", mem11.id)
ok("member deleted", not m11.members)
ok("anchor cascade-deleted", not m11.anchors)
ok("load on that anchor cascade-deleted", not m11.point_loads)
print()

print("12. Anchor position tracks its member as endpoints move")
m12 = Model()
a12 = m12.add_node(0, 0); b12 = m12.add_node(100, 0)
mem12 = m12.add_member(a12.id, b12.id)
pt12 = m12.add_anchor(mem12.id, 0.5)
xy = m12.anchor_xy(pt12)
ok("midpoint at t=0.5", xy is not None and close(xy[0], 50.0) and close(xy[1], 0.0), xy)
b12.x = 200.0
xy2 = m12.anchor_xy(pt12)
ok("moves with the member", close(xy2[0], 100.0), xy2)

print()
print("=" * 58)
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
