# Mininet Quick Start (Project-specific)

This repository does not ship a built-in Mininet topology script. Use `scripts/mininet/mp_topo.py`.

## 1) Build binaries in VM

From VM repo root (`~/Project/4D-MAP`):

```bash
cd ~/Project/4D-MAP
export GO111MODULE=on
go build -o 4dmap .
go build -o qserver ./server
```

## 2) Start Mininet topology

```bash
sudo python3 ~/Project/4D-MAP/scripts/mininet/mp_topo.py
```

In Mininet CLI, verify both links:

```bash
h1 ping -c 2 10.0.1.2
h1 ping -c 2 10.0.2.2
```

## 3) Run server/pull/push in namespaces

Use one run id and one log directory:

```bash
h1 bash -lc 'export RUN_ID=$(date +%Y%m%d_%H%M%S); export LOGDIR=$HOME/Project/4D-MAP/logs_exp/vm_run_${RUN_ID}; mkdir -p "$LOGDIR"; echo RUN_ID=$RUN_ID; echo LOGDIR=$LOGDIR'
```

Copy the printed `RUN_ID` and `LOGDIR`, then use the same values below.

Server on `h2` (run first):

```bash
h2 bash -lc 'export RUN_ID=<RUN_ID>; export LOGDIR=<LOGDIR>; cd ~/Project/4D-MAP/server; ../qserver -protocol=quic -au=false 2>&1 | tee "$LOGDIR/server_${RUN_ID}.log"'
```

Pull on `h1`:

```bash
h1 bash -lc 'export RUN_ID=<RUN_ID>; export LOGDIR=<LOGDIR>; export QUIC_GO_LOG_LEVEL=info; touch ~/Videos/pulled_${RUN_ID}.flv; cd ~/Project/4D-MAP; ./4dmap -type=true -protocol=quic -multi=true -file=~/Videos/pulled_${RUN_ID}.flv rtmp://10.0.1.2/live/test 2>&1 | tee "$LOGDIR/pull_${RUN_ID}.log"'
```

Push on `h1` (or another host namespace with input file):

```bash
h1 bash -lc 'export RUN_ID=<RUN_ID>; export LOGDIR=<LOGDIR>; export QUIC_GO_LOG_LEVEL=info; cd ~/Project/4D-MAP; ./4dmap -type=false -protocol=quic -multi=true -sch=rr -file=~/Videos/push_input.flv rtmp://10.0.1.2/live/test 2>&1 | tee "$LOGDIR/push_${RUN_ID}.log"'
```

## 4) Confirm multipath really happened

```bash
h1 bash -lc 'grep "\[m\]monitor path=" "$LOGDIR"/pull_${RUN_ID}.log | head -20'
h1 bash -lc 'grep "\[utility\] path=" "$LOGDIR"/pull_${RUN_ID}.log | head -20'
```

Expected: both `path=0` and `path=1` appear over time.

If you only see `path=0`, check:
- `-multi=true` is set on both pull and push
- both `10.0.1.x` and `10.0.2.x` are reachable
- you are not using `127.0.0.1`
