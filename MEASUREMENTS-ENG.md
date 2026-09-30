# MEASUREMENTS-ENG.md — "eng.doc": every measurement, engineering detail

The detailed companion to MEASUREMENTS.md (the plain summary). Each
measurement is one numbered section; paragraphs are numbered
section.paragraph. The summary's references in Brett's exact form —
e.g. "RSSI measurement (eng.doc para 3.1)" — point at exactly one
paragraph here. "eng.doc" in the summary means THIS file.

What each fact MEANS is fixed in HEALTH-DEFINITIONS.md. The wire
bytes are fixed in CLINIC-WIRE.md. This page explains how each
number is produced, from which evidence, over which window, and
where it honestly stops.

## 1. The rules under every measurement

1.1 Evidence. Every number comes from frames this box's own radio
heard. Nothing is inferred, smoothed over gaps, or borrowed from
another box's radio.

1.2 Provenance. Every fact carries `source` = the box that measured
or reported it. source == the sending box means Direct (xxxx); any
other source means Reported (xxxx), and reported facts are never
merged into direct ones. Disagreement between boxes is preserved.

1.3 Unknown stays unknown. Wire sentinels mean "unknown", never a
plausible constant: ages 65535, counts 65535, share 255, signal
-128, signal spread 255. A card shows a gap instead of a number.

1.4 Windows tell the truth. Every counting window reports its REAL
length in minutes (window_min on the wire): 60 = a full hour, 1440
= a full day, less = a partial window. A closed complete window is
preferred over a half-finished one.

1.5 No grades. The cards show numbers and evidence only. A trouble
flag is evidence, never a verdict.

## 2. SNR measurement

2.1 What it measures. The signal-to-noise ratio of a node's frames
as THIS box hears them, in dB. It is the link-quality number: how
far the signal sits above the noise floor on received packets.

2.2 How it is collected. Each heard frame's SNR sample is folded
into the node's chart. The chart keeps an exponentially smoothed
value (EWMA), the best sample ever heard, the worst sample ever
heard, and the standard deviation of all samples — the spread.

2.3 Wire and card. Record kind 1 (node fact): snr_ewma, snr_best,
snr_worst as i8 in quarter-dB units (like discover); snr_sd as u8
quarter-dB (255 = unknown; a spread needs at least 2 samples). The
card divides by 4 and draws a FIXED -10..20 dB scale: the pale span
runs worst..best, the white tick sits at the smoothed value, +-spread
reads beside it. No samples = "SNR unknown", never a zero bar.

2.4 Honest limits. The chart is as old as its first hear and lives
until the node is forgotten (the node table's 30-day forget law).
Signal is not distance. A Reported chart is another box's ears and
says so.

## 3. RSSI measurement

3.1 What it measures. The received signal strength of a node's
frames as THIS box hears them, in dBm.

3.2 How it is collected. Exactly like SNR (para 2.2): EWMA, best,
worst, and standard deviation per node chart.

3.3 Wire and card. Record kind 1: rssi_ewma, rssi_best, rssi_worst
as i8 dBm; rssi_sd as u8 dB (255 = unknown). The card draws a FIXED
-120..-70 dBm scale in the same bar form as SNR.

3.4 Honest limits. RSSI is NOT distance — it varies with antenna,
obstruction, and band noise. Never converted to metres anywhere.

## 4. Traffic share measurement

4.1 What it measures. A node's percent of all IDENTIFIED traffic
this box heard in the last 24 hours.

4.2 How it is collected. Per-node hourly buckets hold 24-hour packet
counts. share_pct = node count / all identified counts * 100,
rounded. Identified = traffic that carries a node identity;
anonymous group traffic carries none and counts nowhere (it is
counted out loud and dropped, never invented).

4.3 Wire and card. Record kind 1: share_pct u8 (255 = unknown / no
traffic). The card draws a 10-block bar (1 block = 10%), or the
note "share unknown".

4.4 Honest limits. The share is of traffic THIS box hears and can
identify; a quiet listener undercounts.

## 5. Typical hops measurement

5.1 What it measures. The most common radio hop count of a node's
packets: repeater trail length + 1 (a packet heard straight from
the sender = 1 hop).

5.2 How it is collected. The node's chart keeps a hop histogram;
the typical value is the most frequent hop count (ties go to the
smaller). 0 = unknown, only when no sample ever carried a path.

5.3 Wire and card. Record kind 1: hops_typ u8. The card shows
"Typical hops: N" (or "unknown").

5.4 Honest limits. Hops are counted from the repeater trails this
box sees in headers; a trail the box never hears is invisible.

## 6. Availability strip measurement

6.1 What it measures. Which of the last 24 hours the node was heard
in — the going-and-coming at a glance.

6.2 How it is collected. 24 hourly buckets; a bit is set when the
node was heard at least once in that hour. Bit 0 = the oldest hour
(23 hours ago), bit 23 = the current hour. The card shows the strip
and "heard in N of the last 24 hours" (the set-bit count).

6.3 Persistence. The strip is SQLite write-through: a restart keeps
it (unlike the health records, which are session-scoped by design).

6.4 Honest limits. "Heard" means heard by THIS box at least once in
the hour; an hour is one bit, so ten packets and one packet look
the same.

## 7. Route delay measurement

7.1 What it measures. The end-to-end delay of traffic using one
route: smallest, median, and largest measured, in seconds.

7.2 How it is collected. Honest sender timestamps only — the same
trust rule the route table keeps. The sender stamps its packet;
delay = the moment this box heard it minus the sender's stamp. A
suspicious stamp (timestamps-backwards material) counts nothing.

7.3 Wire and card. Record kind 2 (route fact): delay_min_s,
delay_med_s, delay_max_s as u16, where 0 = unknown (0 never means
"instant"). The card draws one bar on a FIXED 0..15 s scale: span
min..max, tick at the median.

7.4 Honest limits. Only stamped traffic that reaches this box can
be measured; routes used mostly by unstamped traffic show "delay
unknown" instead of numbers.

## 8. Channel occupancy measurement

8.1 What it measures. How much of the air the heard traffic eats:
the share of one hour's airtime that packets consume.

8.2 How it is collected. Every frame heard — every copy — gets its
standard LoRa airtime computed from frame length and this radio's
settings (the same Semtech formula the duty budget uses,
airtime_from_settings). Those airtime values are summed over a
one-hour window (MESH_WINDOW_S = 3600 s) and reported as per-mille
of the window.

8.3 Wire and card. Record kind 5 (airtime fact):
occupancy_per_mille u16, with window_min giving the window's REAL
length. The card shows the percent.

8.4 Honest limits. This is what THIS box can hear. A hidden terminal
whose packets do not reach us is invisible to the count. The health
records are session-scoped: the counter resets at a restart and is
never back-filled from guesses.

## 9. Duty-cycle headroom measurement

9.1 What it measures. How much of OUR OWN allowed sending is left
in the hour.

9.2 How it is collected. The configured duty percentage (e.g. 1%)
gives an airtime allowance per hour (the budget's
duty_allowance_s_per_hour). Headroom = allowance - what we actually
sent. Record kind 5 carries BOTH numbers: duty_headroom_s (the
headroom) and tx_used_s (what we sent).

9.3 Unknown. If the budget cannot say, the field is 65535
(unknown), never a zero.

9.4 Honest limits. Our own TX only — what other nodes spend is not
ours to account.

## 10. Duplicate ratio measurement

10.1 What it measures. How much of the heard traffic is repeats:
repeat copies heard / ALL copies heard in the window — the share of
everything heard (Brett's rule, 2026-09-29). Example: 100 different
packets plus 40 repeat copies = 40 repeats among 140 copies heard
= 29% duplicates.

10.2 How it is collected. Flooding carries every packet once per
repeater path, so the same payload arrives several times; the radio
pipeline's flood dedupe marks each extra copy as a duplicate inside
its dedupe window. Every heard copy — repeat or not — counts in
"everything heard"; a repeat is never counted twice in the sum. A
duplicate is attributed to a sender only when remembered payload
evidence (a hash kept for 120 s) links it to that sender's earlier
packet; otherwise it counts mesh-wide only.

10.3 Wire and card. Record kind 5 (mesh-wide, hour window) and
record kind 6 (per sender, day window): dup_per_mille u16, per
1000, with the window's real length beside it. The per-sender form
is the same share over that sender's copies only.

10.4 Reading rule (Brett). A RISING ratio with a FLAT message count
= the mesh getting noisy, not busier.

10.5 Honest limits. Anonymous traffic carries no sender tag and
never lands in the per-sender record. Retries do not exist on this
radio — nothing retries, so "retry" stays a permanent honest gap.

## 11. Loss and reordering measurement

11.1 What it measures. Per sender, per day: the sender's sequence
numbers this box never heard (lost) and the numbers that arrived
out of order (reordered).

11.2 How it is collected. Every identified packet carries its
sender's 2-byte sequence number. A gap of up to 32 numbers ahead of
the last heard = that many never-heard numbers, counted as lost. A
number arriving after a newer one (within the same 32 window) is
one reorder. A jump larger than 32 (REORDER_JUMP) = a REBOOTED
sender counter: counted as nothing, never as loss. The same number
twice counts as neither.

11.3 Wire and card. Record kind 6 (sender fact): lost, reordered
u16, window_min 1440 = a full day. Counts saturate at 65534.

11.4 Honest limits. This is loss AS WE SEE IT: only senders whose
traffic reaches this box are scored, and a rarely-heard sender shows
gaps nobody actually lost. Retries do not exist on this radio —
nothing retries, so the card's retry gap stays honestly unfilled.

## 12. Neighbor churn (flaps) measurement

12.1 What it measures. How often a sender goes away and comes back:
heard, then silent 30 minutes or more, then heard again = one flap.

12.2 How it is collected. Per sender, a last-heard clock; a silence
of at least 1800 s (FLAP_SILENCE_S) followed by a new hear counts
one flap inside the sender's day window.

12.3 Wire and card. Record kind 6: flaps u16.

12.4 Honest limits. A flap is this box's view of presence — a
sender that went quiet only for US counts the same as one that went
quiet for everyone. The 24-hour strip shows the holes; the flap
count counts the going-and-coming.

## 13. Hash collision measurement

13.1 What it measures. One short tag proven to carry two different
identities.

13.2 How it is collected. Route and path tags are short (1..3
bytes), so two nodes can land on the same tag. A collision is
PROVEN when the SAME tag has carried TWO DIFFERENT advert public
keys on air. Adverts are Ed25519-verified first — a tag that never
met an advert proves nothing and is never counted. The proof is
recorded as the pair: tag, both keys' first 8 bytes, and when the
pair was last proven.

13.3 Wire and card. Record kind 8 (collision fact), 22..24 bytes
per proven pair, capped at 8 pairs (COLLISIONS_MAX) — further pairs
are counted out loud and dropped, never silently. last_age_min =
minutes since the pair was last proven.

13.4 Why it matters. A colliding tag can glue two nodes' identities
together in paths and route display — the pair is the evidence a
suspect tag needs re-naming.

## 14. Request-to-answer measurement

14.1 What it measures. How many overheard asks got an answer back,
and how fast: answered / asked, plus the median answer time.

14.2 How it is collected. An ask heard on the air (a refresh for a
section or route) is remembered for 10 minutes (EXCHANGE_WINDOW_S =
600 s). If that target's data is heard back inside the window =
ANSWERED and the ask-to-answer time is recorded; nothing back =
MISSED. The median answer time is the middle of the recorded times
(0 = unknown).

14.3 Credit rules. "TCP is not the mesh": asks arriving through the
network door never score here. Our own answers never echo back to
our own listener (the flood dedupe suppresses our own TX), so our
own sent burst is credited at send time instead.

14.4 Wire and card. Record kind 7 (exchange fact): asked, answered
u16 and median_answer_s u16 (0 = unknown), window_min honest.

14.5 Honest limits. We hear only what reaches this box: an ask or
answer that never arrives here scores nothing.

## 15. Signature failures (trouble flag 1)

15.1 What it measures. Every advert whose Ed25519 signature check
FAILED, counted against the key prefix the advert claimed.

15.2 What it means. Evidence, never a verdict: a broken node OR
impersonation — the flag does not pick between them.

15.3 Wire. Record kind 3 (trouble flag): flag=1, subject = claimed
key prefix, events u16, first/last ages. Detail is 0.

## 16. Timestamps backwards (trouble flag 2)

16.1 What it measures. A VERIFIED advert stamped EARLIER than the
best stamp already seen for that key, by more than 300 seconds.

16.2 What it means. Replay, reset, or drift — the flag does not
pick. Detail = the worst jump seen, in seconds.

16.3 Wire. Record kind 3: flag=2, subject = key prefix, verified
adverts only.

## 17. Rate storm (trouble flag 3)

17.1 What it measures. 20 or more identity-bearing packets in 60
seconds from one key prefix — far faster than advert cadence.

17.2 Detail = the peak rate, packets per minute. Evidence, never a
verdict.

17.3 Wire. Record kind 3: flag=3, subject = key prefix.

## 18. Corrupt packets (trouble flag 4)

18.1 What it measures. 10% or more of heard packets corrupt over a
10-minute window, with at least 20 packets heard in that window.

18.2 What it means. Band noise or a broken transmitter — NEVER
blamed on a sender. The subject is always 0 (mesh-wide).

18.3 Wire. Record kind 3: flag=4. Detail = the corrupt share,
per-mille.

## 19. Trouble flags: storage and life

19.1 Every flag carries events (how many times measured), the age
of the first event, and the age of the most recent one.

19.2 Flags are SQLite write-through like the node charts. A flag
expires 30 days after its most recent event; the flags of a
forgotten node die with it.

19.3 A flag never says who is "bad" — it says what was measured.
