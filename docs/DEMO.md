# Milestone 2 demonstration checklist

Use a separate console for every process. Keep the three client windows and GFD visible when injecting faults. The commands and machine layout are in the [M2 guide](milestone-2-instructions.md).

| Project guide requirement | What to demonstrate | Implementation |
| --- | --- | --- |
| Steps 1–2 | GFD prints zero members; LFDs register before servers start | GFD registration and heartbeat loop |
| Steps 3–5 | Start S1, S2, S3; membership grows one replica at a time after successful heartbeats | LFD health callbacks and membership reports |
| Steps 6–9 | Each client maintains three TCP connections and sends the same request number to all replicas | Active client broadcast and independent reply readers |
| Step 10 | All three replicas execute requests and reply | M1 state machine retained at each replica |
| Steps 11–12 | Stop S1; LFD1 reports failure; GFD shows S2 and S3 | Local failure detection and delete event |
| Steps 13–14 | Clients continue automatically after the failure | Failed connection retirement |
| Step 15 | First reply delivered; subsequent replies logged as discarded duplicates | Last delivered request number for each client |
| Final check | Stop S2 later; all clients still receive S3 replies | No primary dependency |

Before the demo:

- Install and test the project on each machine using Python 3.11 or later.
- Replace all four example IP addresses in the distributed configuration.
- Verify that the clients can reach each replica and that each LFD can reach the GFD.
- Use the same initial LFD heartbeat period on all three replica machines.
- Start all replicas and confirm three members before starting clients.
- Keep one process per client ID. Do not restart clients independently during the run.

During the demo:

1. Start GFD, then the three LFDs. Point out that LFD registration has not created replica members yet.
2. Start each replica separately and show the membership count changing from zero to three.
3. Start the three automatic clients. Pick one client request number and trace its three sends and three replies.
4. Show the first delivery and the two duplicate-discard messages.
5. Stop S1. Show the failed local heartbeat, the delete report, and the GFD membership change.
6. Let several more requests complete, then stop S2. Show continuing traffic between each client and S3.
7. End the demo without restarting replicas. Restart the complete system for another run.

The guide's M2 rubric explicitly requires automatic continuous clients, even though a general note elsewhere defers client automation. The default M2 client follows that rubric. The guide does not require an RM, checkpoint transfer, or automatic recovery for this milestone.
