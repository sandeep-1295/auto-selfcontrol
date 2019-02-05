#!/usr/bin/env python2.7

import subprocess
import os
import json
import datetime
import syslog
import traceback
import sys
from Foundation import NSUserDefaults, CFPreferencesSetAppValue, CFPreferencesAppSynchronize, NSDate
from pwd import getpwnam
from optparse import OptionParser
from pprint import pprint
import pdb

def convert_block_schedule_to_legacy_format(block_schedules):
    def get_legacy_schedule_block(day=0,sh=0,sm=0,eh=0,em=0):
        return {
            "weekday": day,
            "start-hour": sh,
            "start-minute": sm,
            "end-hour": eh,
            "end-minute": em
        }
    weekdays = [d.strip() for d in """
            monday 
            tuesday
            wednesday
            thursday
            friday
            """.split()]
    weekends = ["saturday", "sunday"]
    weekday_to_int = dict((dayname.strip(), i+1) for i, dayname in enumerate(weekdays+weekends))
    timeslot_dict = {}
    for schedule in block_schedules:
        try:
            start, end = int(schedule['starttime']), int(schedule['endtime'])
            sh,sm, eh,em = start//100, start%100, end//100, end%100
            if not (
                0 <= sh < 24 and
                0 <= eh < 24 and
                0 <= sm < 59 and
                0 <= em < 59
            ):
                raise Exception("not military time")    
        except:
            print sh, sm, eh, em
            exit_with_error("Invalid time entry, has to be military time format")
        start = max(0, min(23*60+59, start//100 * 60 + start %100))
        end = min(23*60+59, end//100 * 60 + end%100)
        if end < start:
            carry_over = end
            end = 23*60+59
        print start, end, carry_over
        days = schedule['days']
        if type(days) in [str, unicode]:
            if days == "everyday":
                days = weekdays + weekends
            elif days == "weekdays":
                days = weekdays
            elif days == "weekends":
                days = weekends
        days = sorted([weekday_to_int[day] for day in days])
        for day in days:
            timeslot_dict.setdefault(day,[])
            timeslot_dict[day].append((start, end))
            if carry_over > 0:
                timeslot_dict.setdefault(day%7+1,[])
                timeslot_dict[day%7+1].append((0, carry_over))
    legacy_block_schedules = []
    for day, timeslots in timeslot_dict.iteritems():
        timeslots.sort()
        for s,e in timeslots:
            block = get_legacy_schedule_block(day, s//60, s%60, e//60, e%60)
            legacy_block_schedules.append(block)
    return legacy_block_schedules


def load_config(config_files, new_format=False):
    """ loads json configuration files
    the latter configs overwrite the previous configs
    """

    config = dict()

    for f in config_files:
        try:
            with open(f, 'rt') as cfg:
                cfg = json.load(cfg)
                if cfg.has_key("new_block_schedule_format") and new_format:
                    block_schedules = cfg['new_block_schedule_format']
                    block_schedules = convert_block_schedule_to_legacy_format(block_schedules)
                    cfg['block-schedules'] = block_schedules
                config.update(cfg)
        except ValueError as e:
            exit_with_error("The json config file {configfile} is not correctly formatted." \
                            "The following exception was raised:\n{exc}".format(configfile=f, exc=e))

    return config


def run(config):
    """ starts self-control with custom parameters, depending on the weekday and the config """

    if check_if_running(config["username"]):
        syslog.syslog(syslog.LOG_ALERT, "SelfControl is already running, ignore current execution of Auto-SelfControl.")
        exit(2)

    try:
        schedule = next(s for s in config["block-schedules"] if is_schedule_active(s))
    except StopIteration:
        syslog.syslog(syslog.LOG_ALERT, "No schedule is active at the moment. Shutting down.")
        exit(0)

    duration = get_duration_minutes(schedule["end-hour"], schedule["end-minute"])

    set_selfcontrol_setting("BlockDuration", duration, config["username"])
    set_selfcontrol_setting("BlockAsWhitelist", 1 if schedule.get("block-as-whitelist", False) else 0,
                            config["username"])

    if schedule.get("host-blacklist", None) is not None:
        set_selfcontrol_setting("HostBlacklist", schedule["host-blacklist"], config["username"])
    elif config.get("host-blacklist", None) is not None:
        set_selfcontrol_setting("HostBlacklist", config["host-blacklist"], config["username"])

    # In legacy mode manually set the BlockStartedDate, this should not be required anymore in future versions
    # of SelfControl.
    if config.get("legacy-mode", True):
        set_selfcontrol_setting("BlockStartedDate", NSDate.date(), config["username"])

    # Start SelfControl
    os.system("{path}/Contents/MacOS/org.eyebeam.SelfControl {userId} --install".format(path=config["selfcontrol-path"], userId=str(getpwnam(config["username"]).pw_uid)))

    syslog.syslog(syslog.LOG_ALERT, "SelfControl started for {min} minute(s).".format(min=duration))


def check_if_running(username):
    """ checks if self-control is already running. """
    defaults = get_selfcontrol_settings(username)
    return defaults.has_key("BlockStartedDate") and not NSDate.distantFuture().isEqualToDate_(defaults["BlockStartedDate"])


def is_schedule_active(schedule):
    """ checks if we are right now in the provided schedule or not """
    currenttime = datetime.datetime.today()
    starttime = datetime.datetime(currenttime.year, currenttime.month, currenttime.day, schedule["start-hour"],
                                  schedule["start-minute"])
    endtime = datetime.datetime(currenttime.year, currenttime.month, currenttime.day, schedule["end-hour"],
                                schedule["end-minute"])
    d = endtime - starttime

    for weekday in get_schedule_weekdays(schedule):
        weekday_diff = currenttime.isoweekday() % 7 - weekday % 7

        if weekday_diff == 0:
            # schedule's weekday is today
            result = starttime <= currenttime and endtime >= currenttime if d.days == 0 else starttime <= currenttime
        elif weekday_diff == 1 or weekday_diff == -6:
            # schedule's weekday was yesterday
            result = d.days != 0 and currenttime <= endtime
        else:
            # schedule's weekday was on any other day.
            result = False

        if result:
            return result

    return False


def get_duration_minutes(endhour, endminute):
    """ returns the minutes left until the schedule's end-hour and end-minute are reached """
    currenttime = datetime.datetime.today()
    endtime = datetime.datetime(currenttime.year, currenttime.month, currenttime.day, endhour, endminute)
    d = endtime - currenttime
    return int(round(d.seconds / 60.0))


def get_schedule_weekdays(schedule):
    """ returns a list of weekdays the specified schedule is active """
    return [schedule["weekday"]] if schedule.get("weekday", None) is not None else range(1, 8)


def set_selfcontrol_setting(key, value, username):
    """ sets a single default setting of SelfControl for the provied username """
    NSUserDefaults.resetStandardUserDefaults()
    originalUID = os.geteuid()
    os.seteuid(getpwnam(username).pw_uid)
    CFPreferencesSetAppValue(key, value, "org.eyebeam.SelfControl")
    CFPreferencesAppSynchronize("org.eyebeam.SelfControl")
    NSUserDefaults.resetStandardUserDefaults()
    os.seteuid(originalUID)


def get_selfcontrol_settings(username):
    """ returns all default settings of SelfControl for the provided username """
    NSUserDefaults.resetStandardUserDefaults()
    originalUID = os.geteuid()
    os.seteuid(getpwnam(username).pw_uid)
    defaults = NSUserDefaults.standardUserDefaults()
    defaults.addSuiteNamed_("org.eyebeam.SelfControl")
    defaults.synchronize()
    result = defaults.dictionaryRepresentation()
    NSUserDefaults.resetStandardUserDefaults()
    os.seteuid(originalUID)
    return result


def get_launchscript(config):
    """ returns the string of the launchscript """
    return '''<?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
        <key>Label</key>
        <string>com.parrot-bytes.auto-selfcontrol</string>
        <key>ProgramArguments</key>
        <array>
            <string>/usr/bin/python</string>
            <string>{path}</string>
            <string>-r</string>
        </array>
        <key>StartCalendarInterval</key>
        <array>
            {startintervals}</array>
        <key>RunAtLoad</key>
        <true/>
    </dict>
    </plist>'''.format(path=os.path.realpath(__file__), startintervals="".join(get_launchscript_startintervals(config)))


def get_launchscript_startintervals(config):
    """ returns the string of the launchscript start intervals """
    entries = list()
    for schedule in config["block-schedules"]:
        for weekday in get_schedule_weekdays(schedule):
            yield ('''<dict>
                    <key>Weekday</key>
                    <integer>{weekday}</integer>
                    <key>Minute</key>
                    <integer>{startminute}</integer>
                    <key>Hour</key>
                    <integer>{starthour}</integer>
                </dict>
                '''.format(weekday=weekday, startminute=schedule['start-minute'], starthour=schedule['start-hour']))


def install(config):
    """ installs auto-selfcontrol """
    print("> Start installation of Auto-SelfControl")

    launchplist_path = "/Library/LaunchDaemons/com.parrot-bytes.auto-selfcontrol.plist"

    # Check for existing plist
    if os.path.exists(launchplist_path):
        print("> Removed previous installation files")
        subprocess.call(["launchctl", "unload", "-w", launchplist_path])
        os.unlink(launchplist_path)

    launchplist_script = get_launchscript(config)

    with open(launchplist_path, 'w') as myfile:
        myfile.write(launchplist_script)

    subprocess.call(["launchctl", "load", "-w", launchplist_path])

    print("> Installed\n")


def check_config(config):
    """ checks whether the config file is correct """
    if not config.has_key("username"):
        exit_with_error("No username specified in config.")
    if config["username"] not in get_osx_usernames():
        exit_with_error(
                "Username '{username}' unknown.\nPlease use your OSX username instead.\n" \
                "If you have trouble finding it, just enter the command 'whoami'\n" \
                "in your terminal.".format(
                        username=config["username"]))
    if not config.has_key("selfcontrol-path"):
        exit_with_error("The setting 'selfcontrol-path' is required and must point to the location of SelfControl.")
    if not os.path.exists(config["selfcontrol-path"]):
        exit_with_error(
                "The setting 'selfcontrol-path' does not point to the correct location of SelfControl. " \
                "Please make sure to use an absolute path and include the '.app' extension, " \
                "e.g. /Applications/SelfControl.app")
    if not config.has_key("block-schedules"):
        exit_with_error("The setting 'block-schedules' is required.")
    if len(config["block-schedules"]) == 0:
        exit_with_error("You need at least one schedule in 'block-schedules'.")
    if config.get("host-blacklist", None) is None:
        print("WARNING:")
        msg = "It is not recommended to directly use SelfControl's blacklist. Please use the 'host-blacklist' " \
              "setting instead."
        print(msg)
        syslog.syslog(syslog.LOG_WARNING, msg)


def get_osx_usernames():
    output = subprocess.check_output(["dscl", ".", "list", "/users"])
    return [s.strip() for s in output.splitlines()]


def excepthook(excType, excValue, tb):
    """ this function is called whenever an exception is not caught """
    err = "Uncaught exception:\n{}\n{}\n{}".format(str(excType), excValue,
                                                   "".join(traceback.format_exception(excType, excValue, tb)))
    syslog.syslog(syslog.LOG_CRIT, err)
    print(err)


def exit_with_error(message):
    syslog.syslog(syslog.LOG_CRIT, message)
    print("ERROR:")
    print(message)
    exit(1)


if __name__ == "__main__":
    # web_pdb.set_trace()
    __location__ = os.path.realpath(os.path.join(os.getcwd(), os.path.dirname(__file__)))
    config = load_config([os.path.join(__location__, "config.json")])
    # pprint(config['block-schedules'])
    # pprint(config)
    # sys.exit()

    sys.excepthook = excepthook

    syslog.openlog("Auto-SelfControl")

    if os.geteuid() != 0:
        exit_with_error("Please make sure to run the script with elevated rights, such as:\nsudo python {file}".format(
                file=os.path.realpath(__file__)))

    parser = OptionParser()
    parser.add_option("-r", "--run", action="store_true",
                      dest="run", default=False)
    (opts, args) = parser.parse_args()
    
    if opts.run:
        run(config)
    else:
        check_config(config)
        install(config)
        if not check_if_running(config["username"]) and any(s for s in config["block-schedules"] if is_schedule_active(s)):
            print("> Active schedule found for SelfControl!")
            print("> Start SelfControl (this could take a few minutes)\n")
            run(config)
            print("\n> SelfControl was started.\n")
