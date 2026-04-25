This python code is meant to help clean up old files on user devices. It determines whether files in a folder need to be pruned by checking for existence of ".prune" file in one of the parent folders. If the .prune file exists, it contains the max age in days; files older than that should be pruned.

One important feature of the pruner: it checks whether the files are synchronized to the backup server. The server can be connected to using ssh.

Rules: 
* Every file being deleted should undergo a check that it has been synchronized to the backup server. Without this check, no file can be deleted.
* Folders should not be deleted, even empty ones.
* .prune files should not be deleted.
* Pruning is done per folder containing .prune files. For each such folder, 
  * all files satisfying the prune condition should be checked against the backup server
  * a user is presented with confirmation "Folder "xxx": allow pruning [y]/n?

File pruning conditions:
* the file must be present on the backup server; 
* the file has to be older than the max age contained in one of the parent's .prune file;
* the user has approved pruning of the containing folder's parent.

Types of supported user devices:
* Android phone with Termux, python3 and ssh installed.

Types of supported backup servers:
* Linux machine with ssh access
