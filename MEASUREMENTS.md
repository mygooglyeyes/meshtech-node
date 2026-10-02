# MEASUREMENTS.md — the health report's numbers, simply

One plain paragraph per measurement. The detailed engineering text
is MEASUREMENTS-ENG.md — "eng.doc" — and every paragraph below
points at its numbered paragraph there in the form "RSSI
measurement (eng.doc para 3.1)".

## SNR margin and spread

The SNR measurement (eng.doc para 2.1) says how clean the node's
signal is as this box hears it: the usual value, the best and worst
ever heard, and how much it wobbles. The card draws it as a bar
from worst to best on a fixed -10 to 20 dB scale, with a tick at
the usual value and the wobble (+-) beside it.

## RSSI margin and spread

The RSSI measurement (eng.doc para 3.1) says how strong the node's
signal is as this box hears it, in dBm: usual value, best and worst
ever heard, and the wobble. On PiMesh boxes the modem first takes the
board's 14 dB amplifier lift back out, so the numbers are true
signal, not the amplified version. The card draws it on a fixed -120
to -70 dBm scale. Signal is not distance — never read it as metres.

## Traffic share

The traffic share measurement (eng.doc para 4.1) says how much of
the identified traffic heard in the last 24 hours belongs to this
node, as a percent. Ten blocks on the card = 100%, one block = 10%.
Traffic that carries no identity counts nowhere.

## Typical hops

The typical hops measurement (eng.doc para 5.1) says how many radio
hops a node's packets usually take to reach this box (the repeater
trail length plus one). It is the hop count heard most often;
"unknown" means no packet ever carried a trail.

## Availability strip

The availability strip measurement (eng.doc para 6.1) shows which
of the last 24 hours the node was heard in: one lit block per hour
it was heard at least once, oldest hour first. "Heard in N of the
last 24 hours" is the count of lit blocks.

## Route delays

The route delay measurement (eng.doc para 7.1) says how long
traffic takes to travel one route end to end: the fastest, the
usual (median), and the slowest measured, in seconds, from honest
sender timestamps. The card draws them on a fixed 0 to 15 s scale.
Zero on the wire means unknown, never instant.

## Channel occupancy

The channel occupancy measurement (eng.doc para 8.1) says how much
of the air the heard traffic eats: the summed airtime of every
packet heard in an hour, as a percent of that hour. It counts what
this box can hear — traffic that never reaches us is invisible.

## Duty-cycle headroom

The duty-cycle headroom measurement (eng.doc para 9.1) says how
much of OUR OWN allowed sending is left in the hour: the allowed
airtime (from the configured duty percent) minus what we actually
sent, in seconds per hour. It is our spending only.

## Duplicate ratio

The duplicate ratio measurement (eng.doc para 10.1) says how much
of the heard traffic is repeats: 40 repeat copies among 140 copies
heard shows "29% duplicates" — a plain share of everything heard.
Reading rule: a RISING ratio with a FLAT message count = the mesh
getting noisy, not busier. Retries do not exist on this radio, so
that card gap stays honestly unfilled.

## Loss and reordering

The loss and reordering measurement (eng.doc para 11.1) counts, per
sender per day, the sequence numbers this box never heard (lost)
and the ones that arrived out of order (reordered). It is loss as
WE see it; a sender's rebooted counter counts as nothing, never as
loss.

## Flaps (neighbor churn)

The flap measurement (eng.doc para 12.1) counts how often a sender
goes away and comes back: heard, then silent 30 minutes or more,
then heard again = one flap. The 24-hour strip shows the holes;
this counts the going-and-coming.

## Hash collisions

The hash collision measurement (eng.doc para 13.1) lists short tags
proven to carry two different identities — the same tag seen with
two different advert public keys. A tag that never met a
signature-checked advert proves nothing and is never counted.

## Request-to-answer

The request-to-answer measurement (eng.doc para 14.1) says how many
overheard asks got an answer back within 10 minutes, and how fast
(median answer time). Door-borne asks (TCP) never score — "TCP is
not the mesh" — and we hear only what reaches this box.

## Signature failures

The signature failures flag (eng.doc para 15.1) counts adverts
whose signature check failed, against the key they claimed. It is
evidence, never a verdict: a broken node OR impersonation — the
flag does not pick.

## Timestamps backwards

The timestamps backwards flag (eng.doc para 16.1) counts verified
adverts stamped earlier than the best stamp already seen for that
key, by more than 5 minutes; it keeps the worst jump in seconds.
Replay, reset, or drift — the flag does not pick.

## Rate storm

The rate storm flag (eng.doc para 17.1) counts one key pushing 20
or more identity-bearing packets in a minute — far faster than
advert cadence — and keeps the peak packets-per-minute. Evidence,
never a verdict.

## Corrupt packets

The corrupt packets flag (eng.doc para 18.1) counts windows where
10% or more of the packets heard were corrupt. It is always
mesh-wide: band noise or a broken transmitter, never blamed on a
sender.

## Noise floor

The noise floor (eng.doc para 20.1) says how loud the channel sits
when nobody is talking, in dBm. The box measures it only in quiet
moments and averages the last 20 kept readings, so the line stays
steady instead of jumping with every stray chirp. A gap means
"not measured yet" — never a made-up number. On PiMesh boxes the
board's 14 dB amplifier lift is taken back out first, like RSSI.
