# HEALTH DEFINITIONS - what each new mesh-health fact MEANS

Written for Brett's line-by-line check (2026-09-29). Nothing here is
code yet. Every number below (windows, thresholds) is a PROPOSAL he
can correct. One rule under all of them: only what the box can
honestly count from the radio. Where a fact cannot be measured, this
page says so and the phone keeps its "not measured yet" gap.

## 1. Duplicate ratio

The same flood packet arrives several times - once per repeater path.
The share of everything heard that is repeats (Brett's pick
2026-09-29):

    repeat copies heard / all copies heard

Example: 100 different packets plus 40 repeat copies = 40 repeats
among 140 copies heard = 29% duplicates.
Reads as: a RISING ratio with a FLAT message count = the mesh getting
noisy, not busier (Brett's words).

## 2. Channel occupancy

How much of the air the heard traffic eats: the standard LoRa airtime
of every packet we hear (same formula the budget uses), added up per
hour, as a percent of one hour. Mesh-wide, per hour.

## 3. Duty-cycle headroom

Our OWN sending only. The allowed duty (config, e.g. 1%) gives an
airtime allowance per hour. Headroom = allowance - what we actually
sent. Reported in seconds per hour.

## 4. Loss and reordering

Every clinic packet carries its box's 2-byte sequence number. Per
box, per day:

- a MISSING number = a packet this box never heard (loss, as WE see
  it),
- a number arriving out of order = a reorder.

Honest limits: we only see loss for boxes whose traffic reaches us,
and "retries" do not exist on this radio - nothing retries, so that
card gap stays honestly unfilled.

## 5. Neighbor churn (flaps)

Per sender: a FLAP = heard, then silent 30 minutes or more, then
heard again the same day. Counted as flaps per sender per day. (The
24-hour strip already shows the holes; this counts the going-and-
coming.)

## 6. Hash collisions

Paths carry short tags (1-3 bytes). A COLLISION = one tag seen
carrying two clearly different identities - two different advert
public keys. We record the pair (tag, both keys, when seen) in a
collision list. A tag that never met an advert proves nothing and is
never counted.

## 7. Request-to-answer success

An overheard exchange: an ask on the air (a refresh for a section or
route) followed by that target's data heard within 10 minutes =
ANSWERED; nothing back within the window = MISSED. Scored per day:
answered / asked, plus the median answer time. Honest limit: we hear
only what reaches this box.

## Flag wording (rides along in the same commit)

The trouble-flag sentences shorten to the phone card's words:
"signature failures", "timestamps backwards" (keeps the worst-jump
number), "rate storm" (keeps the peak number), "corrupt packets"
(keeps the share number). CLINIC-WIRE.md's table follows these names.

## Wire and versions

New clinic records (kinds 5 and up) ride inside the SAME 0x5314
type - 7 records per packet, rotating, as today. CLINIC-WIRE.md gets
the byte tables. meshtech-node 062 -> 063 (Brett's hilltop command),
meshtech-app 034 -> 035 to show the new numbers where the cards say
"not measured yet" today.
