import json
import time

from jinja2 import Template
from paramiko import SSHClient, AutoAddPolicy
from paramiko.ssh_exception import NoValidConnectionsError
from pymongo import MongoClient
from pystalk import BeanstalkClient
from scp import SCPClient

from config import configuration

db_client = MongoClient("mongodb://localhost:27017")
db = db_client["cdn"]

queue = BeanstalkClient("localhost", 11300)

ssh = SSHClient()
ssh.load_system_host_keys()
ssh.set_missing_host_key_policy(AutoAddPolicy())

with open("templates/local.j2", "r") as local_template_file:
    local_template = Template(local_template_file.read())

with open("templates/zone.j2") as zone_template_file:
    zone_template = Template(zone_template_file.read())

with open("templates/default.vcl.j2", "r") as vcl_template_file:
    vcl_template = Template(vcl_template_file.read())

with open("templates/Caddyfile.j2", "r") as caddy_template_file:
    caddy_template = Template(caddy_template_file.read())


def normalize(string: str) -> str:
    return "BACKEND_" + string.rstrip(".").upper().replace("-", "_").replace(".", "_")


def run_ssh_command(command):
    print("    - running " + command, end="", flush=True)
    stdin, stdout, stderr = ssh.exec_command(command)
    for line in stdout:
        print(" - " + line.strip('\n'))
    for line in stderr:
        print(" - ERR " + line.strip('\n'))


print("Starting main loop")

while True:
    for job in queue.reserve_iter():
        content = json.loads(job.job_data)

        operation = content["operation"]
        args = content["args"]

        if operation == "refresh_single_zone":
            print("refreshing " + args["zone"])

            zone = db["zones"].find_one({"zone": args["zone"]})

            zone_file = zone_template.render(
                nameservers=configuration["nameservers"],
                soa_root=configuration["soa_root"],
                records=zone.get("records"),
                serial=zone["serial"],
                proxy4=configuration["proxy4"],
                proxy6=configuration["proxy6"]
            )

            with open("/tmp/db." + zone["zone"], "w") as zone_file_writer:
                zone_file_writer.write(zone_file)

            # Loop over the nodes and send the updated zone file to each one, then reload the configuration
            for node in db["nodes"].find():
                print("... now updating " + node["name"] + " " + node["management_ip"] + " " + node["location"])

                print("    - sending updated zone file")
                try:
                    ssh.connect(node["management_ip"], username="root", port=34553, key_filename=configuration["nodes"]["key"])
                except (TimeoutError, NoValidConnectionsError):
                    error = "- ERROR: " + node["name"] + " timed out."
                    print(error)
                else:
                    with SCPClient(ssh.get_transport()) as scp:
                        scp.put("/tmp/db." + zone["zone"], "/etc/bind/db." + zone["zone"])

                    run_ssh_command("rndc reload")
                    ssh.close()

            print("finished sending updates")

        elif operation == "refresh_zones":
            print("refreshing local zones file")

            zones_file = ""

            # Assemble named.local.conf based on zones
            for zone in db["zones"].find():
                zones_file += local_template.render(zone=zone["zone"])

            # Write the named.conf.local tmp file
            with open("/tmp/named.conf.local", "w") as named_file:
                named_file.write(zones_file)

            # Loop over the nodes and send the updated zone file to each one, then reload the configuration
            for node in db["nodes"].find():
                print("... now updating " + node["name"] + " " + node["management_ip"] + " " + node["location"])

                print("    - sending updated zone file")

                try:
                    ssh.connect(node["management_ip"], username="root", port=34553, key_filename=configuration["nodes"]["key"])
                except (TimeoutError, NoValidConnectionsError):
                    error = "- ERROR: " + node["name"] + " timed out."
                    print(error)
                else:
                    with SCPClient(ssh.get_transport()) as scp:
                        scp.put("/tmp/named.conf.local", "/etc/bind/named.conf.local")

                    run_ssh_command("rndc reload")
                    ssh.close()

            print("finished sending updates")

        elif operation == "delete_zone":
            print("deleting " + args["zone"])

            for node in db["nodes"].find():
                print("... now updating " + node["name"] + " " + node["management_ip"] + " " + node["location"])

                try:
                    ssh.connect(node["management_ip"], username="root", port=34553, key_filename=configuration["nodes"]["key"])
                except (TimeoutError, NoValidConnectionsError):
                    error = "- ERROR: " + node["name"] + " timed out."
                    print(error)
                else:
                    run_ssh_command("rm /etc/bind/db." + args["zone"])
                    ssh.close()

            print("finished deleting " + args["zone"])

        elif operation == "node_power":
            print("setting node " + args["ip"] + " to " + args["state"])

            try:
                ssh.connect(args["ip"], username="root", port=34553, key_filename=configuration["nodes"]["key"])
            except (TimeoutError, NoValidConnectionsError):
                error = "- ERROR: " + args["ip"] + " timed out."
                print(error)
            else:
                if args["state"] == "on":
                    run_ssh_command("systemctl start bird")
                elif args["state"] == "off":
                    run_ssh_command("birdc down")
                ssh.close()

        elif operation == "refresh_cache":
            backends = {}
            domains = {}

            for zone in db["zones"].find():
                for record in zone["records"]:
                    if record.get("proxied"):
                        domain = record["label"].rstrip(".")
                        safe_name = normalize(domain)
                        backends[safe_name] = record["value"]
                        domains[domain] = safe_name

            # Render and write the default.vcl tmp file
            with open("/tmp/default.vcl", "w") as vcl_file:
                vcl_file.write(vcl_template.render(backends=backends, domains=domains))

            # Render and write the Caddyfile tmp file
            with open("/tmp/Caddyfile", "w") as caddy_file:
                caddy_file.write(caddy_template.render(domains=domains))

            # Deploy the vcl file and reload
            for node in db["cache_nodes"].find():
                print("... now updating " + node["name"] + " " + node["management_ip"] + " " + node["location"])
                print("    - sending updated vcl file")

                try:
                    ssh.connect(node["management_ip"], username="root", port=34553, key_filename=configuration["nodes"]["key"])
                except (TimeoutError, NoValidConnectionsError):
                    error = "- ERROR: " + node["name"] + " timed out."
                    print(error)
                else:
                    with SCPClient(ssh.get_transport()) as scp:
                        scp.put("/tmp/default.vcl", "/etc/varnish/default.vcl")
                    with SCPClient(ssh.get_transport()) as scp:
                        scp.put("/tmp/Caddyfile", "/etc/caddy/Caddyfile")

                    run_ssh_command("systemctl reload varnish")
                    run_ssh_command("caddy reload -config /etc/caddy/Caddyfile")
                    ssh.close()

        queue.delete_job(job.job_id)

    time.sleep(0.5)
