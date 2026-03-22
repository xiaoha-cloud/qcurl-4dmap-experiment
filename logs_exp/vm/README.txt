Purpose: committed experiment logs (e.g. from Mininet VM) for shared analysis with the repo.

Add a new run from the VM (replace USER/HOST and paths):

  scp -r mininet@VM:~/Project/4D-MAP/logs_exp/RUN_ID ./logs_exp/vm/run-RUN_ID

Or single files:

  scp mininet@VM:~/Project/4D-MAP/logs_exp/RUN_ID/*.log ./logs_exp/vm/run-RUN_ID/

Then: git add logs_exp/vm/run-RUN_ID && git commit && git push

Quick analysis (after clone/pull on your laptop):

  grep '\[utility\]' logs_exp/vm/run-*/pull*.log
  grep '\[m]monitor' logs_exp/vm/run-*/pull*.log

Folders:
  run-20260317-mininet — sample pull/server logs from a VM-style session
  run-20260312-local — older push/pull/server set
