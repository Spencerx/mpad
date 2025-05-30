#
# Multi-Purpose APRS Daemon: various utility routines
# Author: Joerg Schultze-Lutter, 2020
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

import datetime
import configparser
import os.path
import pytz
import datetime
from timezonefinder import TimezoneFinder
import re
from unidecode import unidecode
import logging
from expiringdict import ExpiringDict
import hashlib
import mpad_config
import zipfile

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(module)s -%(levelname)s- %(message)s"
)
logger = logging.getLogger(__name__)


def make_pretty_aprs_messages(
    message_to_add: str,
    destination_list: list = None,
    max_len: int = 67,
    separator_char: str = " ",
    add_sep: bool = True,
    force_outgoing_unicode_messages: bool = False,
):
    """
    Pretty Printer for APRS messages. As APRS messages are likely to be split
    up (due to the 67 chars message len limitation), this function prevents
    'hard cuts'. Any information that is to be injected into message
    destination list is going to be checked wrt its length. If
    len(current content) + len(message_to_add) exceeds the max_len value,
    the content will not be added to the current list string but to a new
    string in the list.

    Example:

    current APRS message = 1111111111222222222233333333333444444444455555555556666666666

    Add String "Hello World !!!!" (16 chars)

    Add the string the 'conventional' way:

    Message changes to
    Line 1 = 1111111111222222222233333333333444444444455555555556666666666Hello W
    Line 2 = orld !!!!

    This function however returns:
    Line 1 = 1111111111222222222233333333333444444444455555555556666666666
    Line 2 = Hello World !!!!

    In case the to-be-added text exceeds 67 characters due to whatever reason,
    this function first tries to split up the content based on space characters
    in the text and insert the resulting elements word by word, thus preventing
    the program from ripping the content apart. However, if the content consists
    of one or multiple strings which _do_ exceed the maximum text len, then there
    is nothing that we can do. In this case, we will split up the text into 1..n
    chunks of text and add it to the list element.

    Known issues: if the separator_char is different from its default setting
    (space), the second element that is inserted into the list may have an
    additional separator char in the text

    Parameters
    ==========
    message_to_add: 'str'
        message string that is to be added to the list in a pretty way
        If string is longer than 67 chars, we will truncate the information
    destination_list: 'list'
        List with string elements which will be enriched with the
        'mesage_to_add' string. Default: empty list aka user wants new list
    max_len: 'int':
        Max length of the list's string len. 67 for APRS messages
    separator_char: 'str'
        Separator that is going to be used for dividing the single
        elements that the user is going to add
    add_sep: 'bool'
        True = we will add the separator when more than one item
               is in our string. This is the default
        False = do not add the separator (e.g. if we add the
                very first line of text, then we don't want a
                comma straight after the location
    force_outgoing_unicode_messages: 'bool'
        False = all outgoing UTF-8 content will be down-converted
                to ASCII content
        True = all outgoing UTF-8 content will sent out 'as is'

    Returns
    =======
    destination_list: 'list'
        List array, containing 1..n human readable strings with
        the "message_to_add' input data
    """
    # Dummy handler in case the list is completely empty
    # or a reference to a list item has not been specified at all
    # In this case, create an empty list
    if not destination_list:
        destination_list = []

    # replace non-permitted APRS characters from the
    # message text
    # see APRS specification pg. 71
    message_to_add = re.sub("[{}|~]+", "", message_to_add)

    # Check if the user wants unicode messages. Default is ASCII
    if (
        not mpad_config.mpad_enforce_unicode_messages
        and not force_outgoing_unicode_messages
    ):
        # Convert the message to plain ascii
        # Unidecode does not take care of German special characters
        # Therefore, we need to 'translate' them first
        message_to_add = convert_text_to_plain_ascii(message_string=message_to_add)

    # If new message is longer than max len then split it up with
    # max chunks of max_len bytes and add it to the array.
    # This should never happen but better safe than sorry.
    # Keep in mind that we only transport plain text anyway.
    if len(message_to_add) > max_len:
        split_data = message_to_add.split()
        for split in split_data:
            # if string is short enough then add it by calling ourself
            # with the smaller text chunk
            if len(split) < max_len:
                destination_list = make_pretty_aprs_messages(
                    message_to_add=split,
                    destination_list=destination_list,
                    max_len=max_len,
                    separator_char=separator_char,
                    add_sep=add_sep,
                    force_outgoing_unicode_messages=force_outgoing_unicode_messages,
                )
            else:
                # string exceeds max len; split it up and add it as is
                string_list = split_string_to_string_list(
                    message_string=split, max_len=max_len
                )
                for msg in string_list:
                    destination_list.append(msg)
    else:  # try to insert
        # Get very last element from list
        if len(destination_list) > 0:
            string_from_list = destination_list[-1]

            # element + new string > max len? no: add to existing string, else create new element in list
            if len(string_from_list) + len(message_to_add) + 1 <= max_len:
                delimiter = ""
                if len(string_from_list) > 0 and add_sep:
                    delimiter = separator_char
                string_from_list = string_from_list + delimiter + message_to_add
                destination_list[-1] = string_from_list
            else:
                destination_list.append(message_to_add)
        else:
            destination_list.append(message_to_add)

    return destination_list


def split_string_to_string_list(message_string: str, max_len: int = 67):
    """
    Force-split the string into chunks of max_len size and return a list of
    strings. This function is going to be called if the string that the user
    wants to insert exceeds more than e.g. 67 characters. In this unlikely
    case, we may not be able to add the string in a pretty format - but
    we will split it up for the user and ensure that none of the data is lost

    Parameters
    ==========
    message_string: 'str'
        message string that is to be divided into 1..n strings of 'max_len"
        text length
    max_len: 'int':
        Max length of the list's string len. Default = 67 for APRS messages

    Returns
    =======
    split_strings: 'list'
        List array, containing 1..n strings with a max len of 'max_len'
    """
    split_strings = [
        message_string[index : index + max_len]
        for index in range(0, len(message_string), max_len)
    ]
    return split_strings


def check_if_file_exists(file_name: str):
    """
    Simple wrapper for whether a file exists or not

    Parameters
    ==========
    file_name: 'str'
        file whose presence we want to check

    Returns
    =======
    _: 'bool'
        True if file exists
    """

    return os.path.isfile(file_name)


def read_program_config(config_file_name: str = "mpad_api_access_keys.cfg"):
    """
    Read the configuration file and extract the parameter values

    Parameters
    ==========
    config_file_name: 'str'
        file whose presence we want to check

    Returns
    =======
    success: 'bool'
        True if all file exists and there was no issue with extracting
        the values from the config file
    aprsdotfi_cfg_key: 'str'
        aprs.fi API key
    aprsis_login_callsign: 'str'
        Call sign for APRS-IS login
    aprsis_login_passcode: 'str'
        Passcode for APRS-IS login
    dapnet_login_callsign: 'str'
        Call sign for DAPNET login
    dapnet_login_passcode: 'str'
        Passcode for DAPNET login
    smtpimap_email_address: 'str'
        The email address that MPAD is going to use for sending
        e.g. position reports to a mail address
    smtpimap_email_application_password: 'str'
        associated account password.
        If you use GMail, use an app-specific password;
        see https://myaccount.google.com/apppasswords
    apprise_config_file: 'str'
    """

    config = configparser.ConfigParser()
    success = False
    aprsdotfi_cfg_key = apprise_config_file = None
    aprsis_login_callsign = aprsis_login_passcode = None
    dapnet_login_callsign = dapnet_login_passcode = None
    smtpimap_email_address = smtpimap_email_password = None
    if check_if_file_exists(config_file_name):
        try:
            config.read(config_file_name)
            aprsdotfi_cfg_key = config.get("mpad_config", "aprsdotfi_api_key")
            aprsis_login_callsign = config.get("mpad_config", "aprsis_login_callsign")
            aprsis_login_passcode = config.get("mpad_config", "aprsis_login_passcode")
            dapnet_login_callsign = config.get("mpad_config", "dapnet_login_callsign")
            dapnet_login_passcode = config.get("mpad_config", "dapnet_login_passcode")
            smtpimap_email_address = config.get("mpad_config", "smtpimap_email_address")
            smtpimap_email_password = config.get(
                "mpad_config", "smtpimap_email_password"
            )
            apprise_config_file = config.get("mpad_config", "apprise_config_file")
            success = True
        except:
            success = False
    return (
        success,
        aprsdotfi_cfg_key,
        aprsis_login_callsign,
        aprsis_login_passcode,
        dapnet_login_callsign,
        dapnet_login_passcode,
        smtpimap_email_address,
        smtpimap_email_password,
        apprise_config_file,
    )


def getdaysuntil(theweekday):
    """
    Calculate offset index between system date and the requested date offset,
    based on 'calendar' presets (e.g. calendar.MONDAY, calender.TUESDAY)
    In layman's terms: "if current day is Monday, then how many days are
    between Monday and e.g Friday" (result would be '4'; index starts at
    zero).

    If the current day name is equal to the requested day name, we
    return 7 as we assume that this is a reference to the day in the
    next week

    Parameters
    ==========
    theweekday: 'int'
        enum integer, based on 'calendar' enumerations
        e.g. calendar.WEDNESDAY

    Returns
    =======
    _: 'int'
        Number of days between current day and requested day
    """

    today = datetime.date.today()
    target_date = today + datetime.timedelta((theweekday - today.weekday()) % 7)
    if today != target_date:
        return (target_date - today).days
    else:
        return 7


def read_number_of_served_packages(file_name: str = "mpad_served_packages.txt"):
    """
    Read the number of served packages from a file

    If file is not present, we will start with '1'

    Parameters
    ==========
    file_name: 'str'
        Name of the file we are going to read the data from

    Returns
    =======
    served_packages: 'int'
        number of previously served packages (or '1')
    """
    served_packages = 1
    absolute_path_filename = build_full_pathname(file_name=file_name)
    try:
        with open(f"{absolute_path_filename}", "r") as f:
            if f.mode == "r":
                contents = f.read()
                f.close()
                served_packages = int(contents)
    except:
        served_packages = 1
        logger.info(
            msg=f"Cannot read number of served packages from {absolute_path_filename}; will create a new file"
        )
    return served_packages


def write_number_of_served_packages(
    served_packages: int, file_name: str = "mpad_served_packages.txt"
):
    """
    Writes the number of served packages to a file

    Parameters
    ==========
    served_packages: 'int'
        number of previously served packages
    file_name: 'str'
        Name of the file we are going to read the data from

    Returns
    =======
    Nothing
    """
    absolute_path_filename = build_full_pathname(file_name=file_name)
    try:
        with open(f"{absolute_path_filename}", "w") as f:
            f.write("%d" % served_packages)
            f.close()
    except:
        logger.info(
            msg=f"Cannot write number of served packages to {absolute_path_filename}"
        )


def add_aprs_message_to_cache(
    message_text: str, message_no: str, users_callsign: str, aprs_cache: ExpiringDict
):
    """
    Creates an entry in our expiring dictionary cache. Later on,
    we can check for this entry and see if a certain message has already been sent
    within the past x minutes (setting is specified as part of the definition of the
    ExpiringDict). If we find that entry in our list before that entry has expired,
    we will not send it out again and consider the request to be fulfilled

    Parameters
    ==========
    message_text: 'str'
        APRS message (as extracted from the original incoming message)
    message_no: 'str'
        APRS message number (or 'None' if not present)
    users_callsign: 'str'
        Call sign of the user who has sent this message
    aprs_cache: 'ExpiringDict'
        Reference to the ExpiringDict cache

    Returns
    =======
    aprs_cache: 'ExpiringDict'
        Reference to the ExpiringDict cache, now containing our entry
    """
    # Create message key which consists of:
    # - an md5-ed version of the message text (save some bytes on storage)
    #   Conversion to string is necessary; otherwise, the lookup won't work
    # - the user's call sign
    # - the message number (note that this field's content can be 'None')
    md5_hash = hashlib.md5(message_text.encode("utf-8")).hexdigest()
    key = (md5_hash, users_callsign, message_no)
    # Finally, build the key. Convert it to a tuple as the key needs to be immutable
    key = tuple(key)

    # Add the Key to our expiring cache. The datetime stamp is not used; we
    # just need to give the dictionary entry a value
    aprs_cache[key] = datetime.datetime.now()
    return aprs_cache


def get_aprs_message_from_cache(
    message_text: str, message_no: str, users_callsign: str, aprs_cache: ExpiringDict
):
    """
    Checks for an entry in our expiring dictionary cache.
    If we find that entry in our list before that entry has expired,
    MPAD considers the request to be fulfilled and will not process it again

    Parameters
    ==========
    message_text: 'str'
        APRS message (as extracted from the original incoming message)
    message_no: 'str'
        APRS message number (or 'None' if not present)
    users_callsign: 'str'
        Call sign of the user who has sent this message
    aprs_cache: 'ExpiringDict'
        Reference to the ExpiringDict cache

    Returns
    =======
    key: 'Tuple'
        Key tuple (or 'None' if not found / no longer present)
    """
    # Create message key which consists of:
    # - an md5-ed version of the message text (save some bytes on storage)
    #   Conversion to string is necessary; otherwise, the lookup won't work
    # - the user's call sign
    # - the message number (note that this field's content can be 'None')
    md5_hash = hashlib.md5(message_text.encode("utf-8")).hexdigest()
    key = (md5_hash, users_callsign, message_no)
    # Finally, build the key. Convert it to a tuple as the key needs to be immutable
    key = tuple(key)

    if key in aprs_cache:
        return key
    else:
        return None


def dump_string_to_hex(message_text_string: str):
    """
    Converts string to hex format and returns that content to the user.
    If we find that entry in our list before that entry has expired,
    MPAD considers the request to be fulfilled and will not process it again

    Parameters
    ==========
    message_text_string: 'str'
        Text that needs to be converted

    Returns
    =======
    hex-converted text to the user
    """
    return "".join(hex(ord(c))[2:] for c in message_text_string)


def convert_text_to_plain_ascii(message_string: str):
    """
    Converts a string to plain ASCII

    Parameters
    ==========
    message_string: 'str'
        Text that needs to be converted

    Returns
    =======
    hex-converted text to the user
    """
    message_string = (
        message_string.replace("Ä", "Ae")
        .replace("Ö", "Oe")
        .replace("Ü", "Ue")
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )
    message_string = unidecode(message_string)
    return message_string


def read_aprs_message_counter(file_name: str = "mpad_message_counter.txt"):
    """
    Reads the latest message counter from a file

    If file is not present, we will start with '0'

    Parameters
    ==========
    file_name: 'str'
        Name of the file we are going to read the data from

    Returns
    =======
    message_counter: 'int'
        last message counter (or '0')
    """
    served_packages = 0
    absolute_path_filename = build_full_pathname(file_name=file_name)
    try:
        with open(f"{absolute_path_filename}", "r") as f:
            if f.mode == "r":
                contents = f.read()
                f.close()
                served_packages = int(contents)
    except:
        served_packages = 0
        logger.info(
            msg=f"Cannot read content from message counter file {absolute_path_filename}; will create a new file"
        )
    return served_packages


def write_aprs_message_counter(
    aprs_message_counter: int, file_name: str = "mpad_message_counter.txt"
):
    """
    Writes the latest message counter to a file

    Parameters
    ==========
    aprs_message_counter: 'int'
        latest message counter # from file
    file_name: 'str'
        Name of the file we are going to read the data from

    Returns
    =======
    Nothing
    """
    absolute_path_filename = build_full_pathname(file_name=file_name)
    try:
        with open(f"{absolute_path_filename}", "w") as f:
            f.write("%d" % aprs_message_counter)
            f.close()
    except:
        logger.info(msg=f"Cannot write message counter to {absolute_path_filename}")


def build_full_pathname(
    file_name: str,
    root_path_name: str = mpad_config.mpad_root_directory,
    relative_path_name: str = mpad_config.mpad_data_directory,
):
    """
    Build a full-grown path based on $CWD, an optional relative directory name and a file name.

    Parameters
    ==========
    file_name: 'str'
        file name without path
    root_path_name: 'str'
        relative path name that we are going to add.
    relative_path_name: 'str'
        relative path name that we are going to add.

    Returns
    =======
    full_path_name: 'str'
        full path, consisting of root path name, the relative path name and the file name
    """
    return os.path.join(root_path_name, relative_path_name, file_name)


def check_and_create_data_directory(
    root_path_name: str = mpad_config.mpad_root_directory,
    relative_path_name: str = mpad_config.mpad_data_directory,
):
    """
    Check if the data directory is present and create it, if necessary

    Parameters
    ==========
    root_path_name: 'str'
        relative path name that we are going to add.
    relative_path_name: 'str'
        relative path name that we are going to add.

    Returns
    =======
    success: bool
        False in case of error
    """
    success = True
    _data_directory = os.path.join(root_path_name, relative_path_name)
    if not os.path.exists(_data_directory):
        logger.info(
            msg=f"Data directory {_data_directory} does not exist, creating ..."
        )
        try:
            os.mkdir(path=_data_directory)
        except OSError:
            logger.info(
                msg=f"Cannot create data directory {_data_directory}, aborting ..."
            )
            success = False
    else:
        if not os.path.isdir(_data_directory):
            logger.info(msg=f"{_data_directory} is not a directory, aborting ...")
            success = False
    return success


def get_timezone(latitude: float, longitude: float):
    """
    Gets the time zone for the current lat/lon values

    Parameters
    ==========
    latitude: 'float'
        our latitude
    longitude: 'float'
        our longitude

    Returns
    =======
    timezone:
        timezone object
    """

    assert type(latitude) in [float, int]
    assert type(longitude) in [float, int]

    tf = TimezoneFinder()
    timezone_str = tf.timezone_at(lat=latitude, lng=longitude)

    if timezone_str is None:
        logger.debug(
            msg="Could not determine the timezone for the given coordinates; assuming UTC"
        )
        timezone_str = "UTC"

    return pytz.timezone(timezone_str)


def get_local_and_utc_times(latitude: float, longitude: float, base_date: datetime):
    """
    Gets the time zone for the current lat/lon values
    via helper method. Then builds an array for the four
    daytime settings that we have defined in mpad_config
    which are 'night', 'morning', 'daytime', and 'evening'.
    We will then use our knowledge about the user's time zone
    in order to cast the appropriate UTC values from its
    local-time brethren

    Parameters
    ==========
    latitude: 'float'
        our latitude
    longitude: 'float'
        our longitude
    base_date: 'datetime'
        usually, this is equivalent to datetime.utcnow()

    Returns
    =======
    local_and_utc_times: 'dict'
        dictionary with our local time stamps and its UTC counterparts
    """

    # get our time zone
    timezone = get_timezone(latitude=latitude, longitude=longitude)

    # Pre-define our local times for 'night', 'morning', 'daytime', and 'evening'
    local_times = {
        mpad_config.mpad_str_night: datetime.datetime(
            base_date.year,
            base_date.month,
            base_date.day,
            mpad_config.mpad_int_night,
            0,
        ),
        mpad_config.mpad_str_morning: datetime.datetime(
            base_date.year,
            base_date.month,
            base_date.day,
            mpad_config.mpad_int_morning,
            0,
        ),
        mpad_config.mpad_str_daytime: datetime.datetime(
            base_date.year,
            base_date.month,
            base_date.day,
            mpad_config.mpad_int_daytime,
            0,
        ),
        mpad_config.mpad_str_evening: datetime.datetime(
            base_date.year,
            base_date.month,
            base_date.day,
            mpad_config.mpad_int_evening,
            0,
        ),
    }

    local_and_utc_times = {}
    for key, local_time in local_times.items():
        # localize to local time
        local_time = timezone.localize(local_time)

        # convert to utc
        utc_time = local_time.astimezone(pytz.utc)
        utc_offset = local_time.utcoffset()

        local_and_utc_times[key] = {
            "local_time": local_time.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
            "utc_time": utc_time,
            "utc_offset": utc_offset,
        }

    return local_and_utc_times


def find_best_matching_time(
    target_utc_time: datetime,
    timestamp_data: dict,
    timestamp_data_element: str,
    gap_half: int = 3,
):
    """

    Helper method which tries to get the 'best fitting' entry from a list of
    timestamp elements. Whenever data from met.no is getting pulled, the first
    48 hours of data will consist of 48 entries for each hour, followed by
    additional entries for the adjacent days. These additional entries will
    however change to a six hour interval. Additionally, time stamp will be in
    UTC. Dependent on where in the world a user has requested a wx report (and
    for which day), the user might get a direct hit on his wx data query
    (applicable to the 1 hour wx entries) OR the local-time-to-utc translation
    might end up in a gap in case the user has requested a wx forecast for one
    of the days with six hour intervals.

    In order to circumvent this issue, we will check which of the six-hour-interval
    values is the closest to our very own local-time-to-utc values in case the user
    has 'requested a gap value'. Otherwise (= user chooses a value from within the
    next 48 hours), we don't have to do this and select the appropriate value directly.

    Parameters
    ==========
    target_utc_time: 'datetime'
        datetime object
    timestamp_data: 'dict'
        our dictionary with the timestamps
    timestamp_data_element: 'str'
        the element from the dictionary that we intend to access
    gap_half: 'int'
        represents the half of our maximum gap. For the current use
        case, the gap itself is six (6) hours, thus setting
        gap_half for 6/2 = 3

    Returns
    =======
    next_entry: datetime
        best fit for our situation
    """

    half_max_gap = datetime.timedelta(hours=gap_half)
    best_entry = None
    min_diff = datetime.timedelta.max

    for entry in timestamp_data:
        current_diff = entry["timestamp"] - target_utc_time
        if abs(current_diff) < min_diff:
            min_diff = abs(current_diff)
            best_entry = entry

    if min_diff <= half_max_gap:
        # Return the closest entry within the half max gap
        return best_entry

    # If no close entry found within half max gap, find the next
    # greater or smaller entry based on the time difference
    next_greater_entry = None
    next_smaller_entry = None

    for entry in timestamp_data:
        if entry[timestamp_data_element] >= target_utc_time:
            next_greater_entry = entry
            break
        next_smaller_entry = entry

    # Return the appropriate entry based on the condition
    if next_greater_entry and next_smaller_entry:
        if (next_greater_entry[timestamp_data_element] - target_utc_time) < (
            target_utc_time - next_smaller_entry[timestamp_data_element]
        ):
            return next_greater_entry
        else:
            return next_smaller_entry
    elif next_greater_entry:
        return next_greater_entry
    elif next_smaller_entry:
        return next_smaller_entry
    else:
        # No matching entry found
        return None


def create_zip_file_from_log(log_file_name: str):
    """
    Creates a zip file from our current log file and
    returns the file name to the caller

    Parameters
    ==========
    log_file_name: 'str'
        our file name, e.g. 'nohup.out'

    Returns
    =======
    success: 'bool'
        True if we were able to create our zip file, otherwise false
    """

    # Check if the file actually exists
    if not log_file_name:
        return False, None
    if not check_if_file_exists(file_name=log_file_name):
        return False, None

    # get a UTC time stamp as reference and create the file name
    _utc = datetime.datetime.utcnow()
    zip_file_name = datetime.datetime.strftime(
        _utc, "mpad_crash_dump_%Y-%m-%d_%H-%M-%S%z.zip"
    )

    # write the zip file to disk
    with zipfile.ZipFile(zip_file_name, mode="w") as archive:
        archive.write(log_file_name)

    # and return the file name
    return True, zip_file_name


if __name__ == "__main__":
    pass
