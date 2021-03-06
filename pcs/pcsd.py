from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals,
)

import errno
import os
import sys
import time

from pcs import settings
from pcs import usage
from pcs import utils


def pcsd_cmd(argv):
    if len(argv) == 0:
        usage.pcsd()
        sys.exit(1)

    sub_cmd = argv.pop(0)
    if sub_cmd == "help":
        usage.pcsd(argv)
    elif sub_cmd == "certkey":
        pcsd_certkey(argv)
    elif sub_cmd == "sync-certificates":
        pcsd_sync_certs(argv)
    elif sub_cmd == "clear-auth":
        pcsd_clear_auth(argv)
    else:
        usage.pcsd()
        sys.exit(1)

def pcsd_certkey(argv):
    if len(argv) != 2:
        usage.pcsd(["certkey"])
        exit(1)

    certfile = argv[0]
    keyfile = argv[1]

    try:
        with open(certfile, 'r') as myfile:
            cert = myfile.read()
        with open(keyfile, 'r') as myfile:
            key = myfile.read()
    except IOError as e:
        utils.err(e)
    errors = utils.verify_cert_key_pair(cert, key)
    if errors:
        for err in errors:
            utils.err(err, False)
        sys.exit(1)

    if "--force" not in utils.pcs_options and (os.path.exists(settings.pcsd_cert_location) or os.path.exists(settings.pcsd_key_location)):
        utils.err("certificate and/or key already exists, your must use --force to overwrite")

    try:
        try:
            os.chmod(settings.pcsd_cert_location, 0o700)
        except OSError: # If the file doesn't exist, we don't care
            pass

        try:
            os.chmod(settings.pcsd_key_location, 0o700)
        except OSError: # If the file doesn't exist, we don't care
            pass

        with os.fdopen(os.open(settings.pcsd_cert_location, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700), 'w') as myfile:
            myfile.write(cert)

        with os.fdopen(os.open(settings.pcsd_key_location, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700), 'w') as myfile:
            myfile.write(key)

    except IOError as e:
        utils.err(e)

    print("Certificate and key updated, you may need to restart pcsd (service pcsd restart) for new settings to take effect")

def pcsd_sync_certs(argv, exit_after_error=True, async_restart=False):
    error = False
    nodes_sync = argv if argv else utils.getNodesFromCorosyncConf()
    nodes_restart = []

    print("Synchronizing pcsd certificates on nodes {0}...".format(
        ", ".join(nodes_sync)
    ))
    pcsd_data = {
        "nodes": nodes_sync,
    }
    output, retval = utils.run_pcsdcli("send_local_certs", pcsd_data)
    if retval == 0 and output["status"] == "ok" and output["data"]:
        try:
            sync_result = output["data"]
            if sync_result["node_status"]:
                for node, status in sync_result["node_status"].items():
                    print("{0}: {1}".format(node, status["text"]))
                    if status["status"] == "ok":
                        nodes_restart.append(node)
                    else:
                        error = True
            if sync_result["status"] != "ok":
                error = True
                utils.err(sync_result["text"], False)
            if error and not nodes_restart:
                if exit_after_error:
                    sys.exit(1)
                else:
                    return
        except (KeyError, AttributeError):
            utils.err("Unable to communicate with pcsd", exit_after_error)
            return
    else:
        utils.err("Unable to sync pcsd certificates", exit_after_error)
        return

    print("Restarting pcsd on the nodes in order to reload the certificates...")
    pcsd_restart_nodes(
        nodes_restart, exit_after_error, async_restart=async_restart
    )

def pcsd_clear_auth(argv):
    output = []
    files = []
    if os.geteuid() == 0:
        pcsd_tokens_file = settings.pcsd_tokens_location
    else:
        pcsd_tokens_file = os.path.expanduser("~/.pcs/tokens")

    if '--local' in utils.pcs_options:
        files.append(pcsd_tokens_file)
    if '--remote' in utils.pcs_options:
        files.append(settings.pcsd_users_conf_location)

    if len(files) == 0:
        files.append(pcsd_tokens_file)
        files.append(settings.pcsd_users_conf_location)

    for f in files:
        try:
            os.remove(f)
        except OSError as e:
            if (e.errno != errno.ENOENT):
                output.append(e.strerror + " (" + f + ")")

    if len(output) > 0:
        for o in output:
            print("Error: " + o)
        sys.exit(1)

def pcsd_restart_nodes(nodes, exit_after_error=True, async_restart=False):
    pcsd_data = {
        "nodes": nodes,
    }
    instance_signatures = dict()

    error = False
    output, retval = utils.run_pcsdcli("pcsd_restart_nodes", pcsd_data)
    if retval == 0 and output["status"] == "ok" and output["data"]:
        try:
            restart_result = output["data"]
            if restart_result["node_status"]:
                for node, status in restart_result["node_status"].items():
                    # If the request got accepted and we have the instance
                    # signature, we are able to check if the restart was
                    # perfirmed. Otherwise we just print the status. Instance
                    # signature got added in pcs-0.9.156.
                    if status["status"] == "ok":
                        sign = status.get("instance_signature", "")
                        if sign:
                            instance_signatures[node] = sign
                            continue
                    print("{0}: {1}".format(node, status["text"]))
                    if status["status"] != "ok":
                        error = True
            if restart_result["status"] != "ok":
                error = True
                utils.err(restart_result["text"], False)
            if error:
                if exit_after_error:
                    sys.exit(1)
                else:
                    return
        except (KeyError, AttributeError):
            utils.err("Unable to communicate with pcsd", exit_after_error)
            return
    else:
        utils.err("Unable to restart pcsd", exit_after_error)
        return

    if async_restart:
        print("Not waiting for restart of pcsd on all nodes.")
        return

    # check if the restart was performed already
    error = False
    for _ in range(5):
        if not instance_signatures:
            # no more nodes to check
            break
        time.sleep(2)
        for node, signature in list(instance_signatures.items()):
            retval, output = utils.getPcsdInstanceSignature(node)
            if retval == 0 and signature != output:
                del instance_signatures[node]
                print("{0}: Success".format(node))
            elif retval in (3, 4):
                # node not authorized or permission denied
                del instance_signatures[node]
                utils.err(output, False)
                error = True
            # if connection refused or http error occurs the dameon is just
            # restarting so we'll try it again
    if instance_signatures:
        for node in sorted(instance_signatures.keys()):
            utils.err("{0}: Not restarted".format(node), False)
            error = True
    if error and exit_after_error:
        sys.exit(1)

