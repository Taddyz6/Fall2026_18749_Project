# Milestone 2 design

M2 keeps the M1 application: C1, C2, and C3 each increment their own counter. The state machine, TCP message framing, and local replica heartbeat loop are reused. The new work connects each client to three replicas and adds a GFD to collect replica health information.

## Client requests

Each client opens one TCP connection to each of S1, S2, and S3. For request n, it sends the same increment and request number to all three replicas. Only the destination replica ID changes. It finishes sending this request before sending request n+1.

A separate asyncio task reads replies from each replica. The first valid reply completes the request. Replies with a request number already delivered are printed and discarded. If a duplicate's counter value differs from the delivered value, the client also logs a replica-state-divergence warning. Keeping the readers separate lets the client receive late replies without blocking later requests. A failed connection is removed, and the client continues using the remaining connections.

Both M1 and M2 clients run in a loop by default. The optional `--interactive` flag permits manual requests, and `--count` ends a run after a fixed number of successful requests for testing.

## Replica processing

Each replica runs the original M1 state machine and uses the original processing lock. It prints state before and after the increment and replies directly to the client. The GFD does not handle application requests. Heartbeats do not change application state.

There is one additional error-handling check on the server: it saves the last request number and reply for each client. If a client retries after a lost or late reply, the replica returns the saved reply instead of incrementing twice. This is an in-memory dictionary, not checkpointing or recovery. Each client ID must belong to one running process, and clients should not be restarted independently during the demo.

## Fault detection

Each LFD runs on the same machine as its replica. It uses the M1 heartbeat connection and reports health changes to the GFD. It can start before the replica and retry until the replica is available.

The LFD registers with the GFD over TCP. Registration alone does not add a replica. After the first successful local heartbeat, the LFD sends an add event. If the replica later fails a heartbeat, the LFD sends a delete event. Repeated failures do not repeatedly remove the same member.

The GFD maintains the member IDs and count and prints them when they change. It also sends numbered heartbeats to each registered LFD. If the LFD disconnects or stops answering, the GFD removes its replica, following the project assumption that LFD failure means machine failure.

The LFD replies to GFD heartbeats and sends membership updates over the same TCP connection. A lock keeps these writes from overlapping. If that connection breaks, the LFD reconnects and reports its current health. This reconnects monitoring; it does not recover replica state.

Heartbeat periods are measured in seconds. Keep GFD timing consistent in the shared configuration: a large GFD-only command-line override can exceed the receive timeout configured at the LFD.

## State consistency

Each client changes only its own counter, so increments from different clients do not interfere. Requests from one client stay ordered on each TCP connection. Once replicas have processed the same unique requests, their counter states are equal. The tests check this after all replicas finish the same requests.

TCP does not provide a single order across different client connections. While requests are still in transit, the full state printed by different replicas may differ. This implementation relies on the independent counters already used in M1; it does not add a general ordering algorithm.

## Demo scope

Start all three replicas before starting the clients. Stop two replicas one at a time while leaving the clients running. The remaining replica continues to handle requests.

Do not restart a failed replica during this demo. It would start with empty state, and M2 does not include state transfer. Restart the whole system for a fresh run. Passive replication, the Replication Manager, checkpointing, automated recovery, and persistent storage are not implemented.
