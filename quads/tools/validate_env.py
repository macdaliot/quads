#!/usr/bin/python3
import argparse
import asyncio
import logging
import os
import socket

from datetime import datetime
from jinja2 import Template
from paramiko import SSHException
from paramiko.ssh_exception import NoValidConnectionsError

from quads.config import conf, TEMPLATES_PATH, INTERFACES
from quads.model import Cloud, Schedule, Host, Notification
from quads.tools.badfish import BadfishException, badfish_factory
from quads.tools.foreman import Foreman
from quads.tools.postman import Postman
from quads.tools.ssh_helper import SSHHelper

logger = logging.getLogger(__name__)


class Validator(object):
    def __init__(self, cloud):
        self.cloud = cloud
        self.report = ""
        self.hosts = Host.objects(cloud=self.cloud, validated=False)
        self.hosts = [
            host for host in self.hosts if Schedule.current_schedule(host=host)
        ]

    def notify_failure(self):
        template_file = "validation_failed"
        with open(os.path.join(TEMPLATES_PATH, template_file)) as _file:
            template = Template(_file.read())
        parameters = {
            "cloud": self.cloud.name,
            "owner": self.cloud.owner,
            "ticket": self.cloud.ticket,
            "report": self.report,
        }
        content = template.render(**parameters)

        subject = "Validation check failed for {cloud} / {owner} / {ticket}".format(
            **parameters
        )
        _cc_users = conf["report_cc"].split(",")
        postman = Postman(subject, "dev-null", _cc_users, content)
        postman.send_email()

    def notify_success(self):
        template_file = "validation_succeeded"
        with open(os.path.join(TEMPLATES_PATH, template_file)) as _file:
            template = Template(_file.read())
        parameters = {
            "cloud": self.cloud.name,
            "owner": self.cloud.owner,
            "ticket": self.cloud.ticket,
        }
        content = template.render(**parameters)

        subject = "Validation check succeeded for {cloud} / {owner} / {ticket}".format(
            **parameters
        )
        _cc_users = conf["report_cc"].split(",")
        postman = Postman(subject, "dev-null", _cc_users, content)
        postman.send_email()

    def env_allocation_time_exceeded(self):
        now = datetime.now()
        schedule = Schedule.objects(
            cloud=self.cloud, start__lt=now, end__gt=now
        ).first()
        time_delta = now - schedule.start
        if time_delta.seconds // 60 > conf["validation_grace_period"]:
            return True
        logger.warning(
            "You're still within the configurable validation grace period. Skipping validation for %s."
            % self.cloud.name
        )
        return False

    def post_system_test(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        password = f"{conf['infra_location']}@{self.cloud.ticket}"
        foreman = Foreman(
            conf["foreman_api_url"], self.cloud.name, password, loop=loop,
        )

        if not loop.run_until_complete(foreman.verify_credentials()):
            logger.error("Unable to query Foreman for cloud: %s" % self.cloud.name)
            logger.error("Verify Foreman password is correct: %s" % password)
            self.report = (
                self.report
                + "Unable to query Foreman for cloud: %s\n" % self.cloud.name
            )
            self.report = (
                self.report + "Verify Foreman password is correct: %s\n" % password
            )
            return False

        build_hosts = loop.run_until_complete(foreman.get_build_hosts())

        pending = []
        schedules = Schedule.current_schedule(cloud=self.cloud)
        if schedules:
            for schedule in schedules:
                if schedule.host and schedule.host.name in build_hosts:
                    pending.append(schedule.host.name)

            if pending:
                logger.info(
                    "The following hosts are marked for build and will now be rebooted:"
                )
                self.report = (
                    self.report
                    + "The following hosts are marked for build:\n"
                )
                for host in pending:
                    logger.info(host)
                    try:
                        badfish = loop.run_until_complete(
                            badfish_factory(
                                "mgmt-" + host,
                                str(conf["ipmi_username"]),
                                str(conf["ipmi_password"]),
                            )
                        )
                        loop.run_until_complete(badfish.set_next_boot_pxe())
                        loop.run_until_complete(badfish.reboot_server())
                    except BadfishException:
                        logger.info(f"Could not run reboot for: {host}")
                    self.report = self.report + "%s\n" % host
                return False

        failed = False
        for host in self.hosts:
            try:
                badfish = loop.run_until_complete(
                    badfish_factory(
                        "mgmt-" + host.name, str(conf["ipmi_cloud_username"]), password,
                    )
                )
                loop.run_until_complete(badfish.validate_credentials())
            except BadfishException:
                logger.info(f"Could not verify badfish credentials for: {host.name}")
                failed = True

        return not failed

    def post_network_test(self):
        test_host = self.hosts[0]
        for host in self.hosts:
            if len(host.interfaces) > len(test_host.interfaces):
                test_host = host
        try:
            ssh_helper = SSHHelper(test_host.name)
        except (SSHException, NoValidConnectionsError, socket.timeout) as ex:
            logger.debug(ex)
            logger.error(
                "Could not establish connection with host: %s." % test_host.name
            )
            self.report = (
                self.report
                + "Could not establish connection with host: %s.\n" % test_host.name
            )
            return False
        host_list = " ".join([host.name for host in self.hosts])

        if type(ssh_helper.run_cmd("fping -u %s" % host_list)) != list:
            return False

        for i, interface in enumerate(INTERFACES.keys()):
            new_ips = []
            host_ips = [
                {"ip": socket.gethostbyname(host.name), "host": host}
                for host in self.hosts
                if interface in [_interface.name for _interface in host.interfaces]
            ]
            for host in host_ips:
                _host_obj = host["host"]
                _interfaces = INTERFACES[interface]
                last_nic = i == len(_host_obj.interfaces) - 1
                if last_nic and self.cloud.vlan:
                    _interfaces = _interfaces[:1]
                for value in _interfaces:
                    ip_apart = host["ip"].split(".")
                    octets = value.split(".")
                    ip_apart[0] = octets[0]
                    ip_apart[1] = octets[1]
                    new_ips.append(".".join(ip_apart))

            if new_ips:
                if type(ssh_helper.run_cmd("fping -u %s" % " ".join(new_ips))) != list:
                    return False

        ssh_helper.disconnect()

        return True

    def validate_env(self):
        logger.info(f"Validating {self.cloud.name}")
        notification_obj = Notification.objects(
            cloud=self.cloud, ticket=self.cloud.ticket
        ).first()
        failed = False

        if self.env_allocation_time_exceeded():
            if self.hosts:
                if not self.post_system_test():
                    failed = True

                if not failed and not self.post_network_test():
                    failed = True

            # TODO: gather ansible-cmdb facts

            # TODO: quads dell config report

            if not failed:
                if not notification_obj.success:
                    self.notify_success()
                    notification_obj.update(success=True, fail=False)

                for host in self.hosts:
                    host.update(validated=True)
                self.cloud.update(validated=True)

        if failed and not notification_obj.fail:
            self.notify_failure()
            notification_obj.update(fail=True)

        return


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate Quads assignments")
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Show debugging information.",
    )
    args = parser.parse_args()

    level = logging.INFO
    if args.debug:
        level = logging.DEBUG

    logging.basicConfig(level=level, format="%(message)s")

    clouds = Cloud.objects(validated=False, provisioned=True, name__ne="cloud01")
    for _cloud in clouds:
        _schedule_count = Schedule.current_schedule(cloud=_cloud).count()
        if _schedule_count and _cloud.wipe:
            validator = Validator(_cloud)
            try:
                validator.validate_env()
            except Exception as ex:
                logger.debug(ex)
                logger.info("Failed validation for %s" % _cloud.name)
