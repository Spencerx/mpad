#
# Multi-Purpose APRS Daemon: Command parser
# Author: Joerg Schultze-Lutter, 2020
#
# Purpose: Core input parser. Takes a look at the command that the user
# the user has sent to us and then tries to figure out what to do
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

import re
import maidenhead
from geopy_modules import (
    get_reverse_geopy_data,
    get_geocode_geopy_data,
    validate_country,
)
import calendar
import string
from airport_data_modules import validate_icao, validate_iata, get_nearest_icao
from utility_modules import getdaysuntil, read_program_config
from aprsdotfi_modules import get_position_on_aprsfi
import logging
from datetime import datetime
import mpad_config
from pprint import pformat

aprsdotfi_api_key = None

errmsg_cannot_find_coords_for_address: str = (
    "Cannot find coordinates for requested address"
)
errmsg_cannot_find_coords_for_user: str = "Cannot find coordinates for callsign"
errmsg_invalid_country: str = "Invalid country code (need ISO3166-a2)"
errmsg_invalid_state: str = "Invalid US state"
errmsg_invalid_command: str = (
    "Unknown command. See https://github.com/joergschultzelutter/mpad"
)
errmsg_no_satellite_specified: str = "No satellite specified"
errmsg_no_cwop_specified: str = "No cwop id specified"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(module)s -%(levelname)s- %(message)s"
)
logger = logging.getLogger(__name__)


def parse_input_message(aprs_message: str, users_callsign: str, aprsdotfi_api_key: str):
    """
    Core parser. Takes care of analyzing the user's request and tries to
    figure out what has been requested (weather report, position report, ..)

    Parameters
    ==========
    aprs_message : 'str'
        up to 67 bytes of content that the user has submitted to us
    users_callsign : 'str'
        User's ham radio call sign that was used to submit the message to us
    aprsdotfi_api_key: 'str'
        APRS.fi access key

    Returns
    =======
    success: Bool
        False in case an error has occurred
    response_parameters: dict
        Dictionary, containing a lot of keyword-specific content for the
        keyword-specific output processing
    """
    # default settings for units of measure and language
    units = "metric"
    language = "en"

    # initialize the fields that we intend to parse
    # default 'an error has occurred' marker
    err = False

    latitude = longitude = altitude = users_latitude = users_longitude = 0.0
    date_offset = -1  # Date offset ("Monday", "tomorrow" etc) for wx fc
    hour_offset = -1  # Hour offset (1h, 2h, 3h, ...) for wx fc

    # If a keyword potentially returns more than one entry, we permit the user
    # to see up to 5 results per query. Default is "1". Value can be overridden
    # by "top2" ... "top5" key words
    number_of_results = 1

    lasttime = datetime.min  # Placeholder in case lasttime is not present on aprs.fi

    when = when_daytime = what = city = state = country = zipcode = cwop_id = None
    icao = human_readable_message = satellite = repeater_band = repeater_mode = None
    street = street_number = county = osm_special_phrase = dapnet_message = None
    mail_recipient = country_code = district = address = comment = None

    # Call sign reference (either the user's call sign or someone
    # else's call sign
    message_callsign = None

    # Force UTF-8 output messages switch
    force_outgoing_unicode_messages = False

    # This is the general 'we have found something and we know what to do'
    # marker. If set to true, it will prevent any further attempts to parse other
    # parts of the message wrt position information (first-come-first-serve)
    found_my_duty_roster = False

    # Booleans for 'what information were we able to retrieve from the msg'
    found_when = found_when_daytime = False

    #
    # Start the parsing process
    #
    # Convert user's call sign to uppercase
    users_callsign = users_callsign.upper()

    # Check if we need to switch to the imperial system ...
    # Note: this is not an action keyword so we don't set the duty roster flag
    units = get_units_based_on_users_callsign(users_callsign=users_callsign)
    #
    # ... and then check if the user wants to override this default setting
    # because he said so in his command to us
    # Note: this is not an action keyword so we don't set the duty roster flag
    found_my_keyword, parser_rd_units = parse_keyword_units(aprs_message=aprs_message)
    if found_my_keyword:
        aprs_message = parser_rd_units["aprs_message"]
        units = parser_rd_units["units"]

    # Check if the user explicitly requests unicode messages
    found_my_keyword, parser_rd_unicode = parse_keyword_unicode(
        aprs_message=aprs_message
    )
    if found_my_keyword:
        aprs_message = parser_rd_unicode["aprs_message"]
        force_outgoing_unicode_messages = parser_rd_unicode[
            "force_outgoing_unicode_messages"
        ]

    # check if the user wants to change the language
    # Note: this is not an action keyword so we don't set the duty roster flag
    found_my_keyword, parser_rd_language = parse_keyword_language(
        aprs_message=aprs_message
    )
    if found_my_keyword:
        aprs_message = parser_rd_language["aprs_message"]
        language = parser_rd_language["language"]

    # check if the user wants more than one result (if supported by respective keyword)
    # hint: setting is not tied to the program's duty roster
    # Note: this is not an action keyword so we don't set the duty roster flag
    found_my_keyword, parser_rd_number_of_results = parse_keyword_number_of_results(
        aprs_message=aprs_message
    )
    if found_my_keyword:
        aprs_message = parser_rd_number_of_results["aprs_message"]
        number_of_results = parser_rd_number_of_results["number_of_results"]

    # check if the user wants to receive the MPAD help pages
    # in order to avoid any mishaps, we will detect this keyword
    # only if it is the ONLY content in the user's message
    if not found_my_duty_roster and not err:
        matches = re.search(
            pattern=r"^(info|help)$", string=aprs_message, flags=re.IGNORECASE
        )
        if matches and not what:
            what = "help"
            found_my_duty_roster = True

    # Now let's start with examining the message text.
    # Rule of thumb:
    # 1) the FIRST successful match will prevent
    # parsing of *location*-related information
    # 2) If we find some data in this context, then it will
    # be removed from the original message in order to avoid
    # any additional occurrences at a later point in time.
    #
    # First, start with parsing the when/when_daytime content.
    # As all of this data is keyword-less, we need to process and
    # -in case the processing was successful- remove it from the
    # original APRS string as it might otherwise get misinterpreted
    # for e.g. call sign data
    #
    # The parser itself is far from perfect. It may get confused by
    # remaining content, thus forcing it back to its default mode (wx).
    # So we need to avoid that (if possible)
    #
    # Parse the "when" information if we don't have an error
    # and if we haven't retrieved the command data in a previous run
    if not found_when and not err and not found_my_duty_roster:
        _msg, found_when, when, date_offset, hour_offset = parse_when(aprs_message)
        # if we've found something, then replace the original APRS message with
        # what is left of it (minus the parts that were removed by the parse_when
        # parser code
        if found_when:
            aprs_message = _msg

    # Parse the "when_daytime" information if we don't have an error
    # and if we haven't retrieved the command data in a previous run
    if not found_when_daytime and not err and not found_my_duty_roster:
        _msg, found_when_daytime, when_daytime = parse_when_daytime(aprs_message)
        # if we've found something, then replace the original APRS message with
        # what is left of it (minus the parts that were removed by the parse_when
        # parser code
        if found_when_daytime:
            aprs_message = _msg

    # Check if the user wants one of the following info
    # for a specific call sign WITH or withOUT SSID:
    # wx (Weather report for the user's position)
    # whereis (location information for the user's position)
    # riseset (Sunrise/Sunset and moonrise/moonset info)
    # metar (nearest METAR data for the user's position)
    # taf (nearest TAF date for the user's position)
    # CWOP (nearest CWOP data for user's position)
    #
    if not found_my_duty_roster and not err:
        found_my_keyword, kw_err, parser_rd_csm = parse_what_keyword_callsign_multi(
            aprs_message=aprs_message,
            users_callsign=users_callsign,
            aprsdotfi_api_key=aprsdotfi_api_key,
            language=language,
        )
        if found_my_keyword or kw_err:
            found_my_duty_roster = found_my_keyword
            err = kw_err
            latitude = parser_rd_csm["latitude"]
            longitude = parser_rd_csm["longitude"]
            users_latitude = parser_rd_csm["users_latitude"]
            users_longitude = parser_rd_csm["users_longitude"]
            lasttime = parser_rd_csm["lasttime"]
            comment = parser_rd_csm["comment"]
            altitude = parser_rd_csm["altitude"]
            what = parser_rd_csm["what"]
            human_readable_message = parser_rd_csm["human_readable_message"]
            aprs_message = parser_rd_csm["aprs_message"]
            message_callsign = parser_rd_csm["message_callsign"]
            city = parser_rd_csm["city"]
            state = parser_rd_csm["state"]
            county = parser_rd_csm["county"]
            country = parser_rd_csm["country"]
            country_code = parser_rd_csm["country_code"]
            district = parser_rd_csm["district"]
            address = parser_rd_csm["address"]
            zipcode = parser_rd_csm["zipcode"]
            street = parser_rd_csm["street"]
            street_number = parser_rd_csm["street_number"]
            icao = parser_rd_csm["icao"]

    # User wants to know his own position? (WHEREAMI keyword)
    if not found_my_duty_roster and not err:
        found_my_keyword, kw_err, parser_rd_whereami = parse_what_keyword_whereami(
            aprs_message=aprs_message,
            users_callsign=users_callsign,
            aprsdotfi_api_key=aprsdotfi_api_key,
            language=language,
        )
        if found_my_keyword or kw_err:
            found_my_duty_roster = found_my_keyword
            err = kw_err
            latitude = parser_rd_whereami["latitude"]
            longitude = parser_rd_whereami["longitude"]
            users_latitude = parser_rd_whereami["users_latitude"]
            users_longitude = parser_rd_whereami["users_longitude"]
            lasttime = parser_rd_whereami["lasttime"]
            comment = parser_rd_whereami["comment"]
            altitude = parser_rd_whereami["altitude"]
            what = parser_rd_whereami["what"]
            human_readable_message = parser_rd_whereami["human_readable_message"]
            aprs_message = parser_rd_whereami["aprs_message"]
            message_callsign = parser_rd_whereami["message_callsign"]
            city = parser_rd_whereami["city"]
            state = parser_rd_whereami["state"]
            county = parser_rd_whereami["county"]
            country = parser_rd_whereami["country"]
            country_code = parser_rd_whereami["country_code"]
            district = parser_rd_whereami["district"]
            address = parser_rd_whereami["address"]
            zipcode = parser_rd_whereami["zipcode"]
            street = parser_rd_whereami["street"]
            street_number = parser_rd_whereami["street_number"]

    # Check if the user wants information about a specific CWOP ID
    if not found_my_duty_roster and not err:
        found_my_keyword, kw_err, parser_rd_cwop_id = parse_what_keyword_cwop_id(
            aprs_message=aprs_message, users_callsign=users_callsign
        )
        if found_my_keyword or kw_err:
            found_my_duty_roster = found_my_keyword
            err = kw_err
            what = parser_rd_cwop_id["what"]
            message_callsign = parser_rd_cwop_id["message_callsign"]
            cwop_id = parser_rd_cwop_id["cwop_id"]
            human_readable_message = parser_rd_cwop_id["human_readable_message"]
            aprs_message = parser_rd_cwop_id["aprs_message"]

    # Check if the user wants to gain information about an upcoming satellite pass
    if not found_my_duty_roster and not err:
        found_my_keyword, kw_err, parser_rd_satpass = parse_what_keyword_satpass(
            aprs_message=aprs_message,
            users_callsign=users_callsign,
            aprsdotfi_api_key=aprsdotfi_api_key,
        )
        if found_my_keyword or kw_err:
            found_my_duty_roster = found_my_keyword
            err = kw_err
            what = parser_rd_satpass["what"]
            latitude = parser_rd_satpass["latitude"]
            longitude = parser_rd_satpass["longitude"]
            altitude = parser_rd_satpass["altitude"]
            lasttime = parser_rd_satpass["lasttime"]
            comment = parser_rd_satpass["comment"]
            message_callsign = parser_rd_satpass["message_callsign"]
            satellite = parser_rd_satpass["satellite"]
            human_readable_message = parser_rd_satpass["human_readable_message"]
            aprs_message = parser_rd_satpass["aprs_message"]

    # Search for repeater-mode-band
    if not found_my_duty_roster and not err:
        (
            found_my_keyword,
            kw_err,
            parser_rd_repeater,
        ) = parse_what_keyword_repeater(
            aprs_message=aprs_message,
            users_callsign=users_callsign,
            aprsdotfi_api_key=aprsdotfi_api_key,
        )
        # did we find something? Then overwrite the existing variables with the retrieved content
        if found_my_keyword or kw_err:
            found_my_duty_roster = found_my_keyword
            err = kw_err
            what = parser_rd_repeater["what"]
            latitude = parser_rd_repeater["latitude"]
            longitude = parser_rd_repeater["longitude"]
            altitude = parser_rd_repeater["altitude"]
            lasttime = parser_rd_repeater["lasttime"]
            comment = parser_rd_repeater["comment"]
            message_callsign = parser_rd_repeater["message_callsign"]
            repeater_band = parser_rd_repeater["repeater_band"]
            repeater_mode = parser_rd_repeater["repeater_mode"]
            human_readable_message = parser_rd_repeater["human_readable_message"]
            aprs_message = parser_rd_repeater["aprs_message"]

    # Check for an OpenStreetMap category (e.g. supermarket, police)
    if not found_my_duty_roster and not err:
        found_my_keyword, kw_err, parser_rd_osm = parse_what_keyword_osm_category(
            aprs_message=aprs_message,
            users_callsign=users_callsign,
            aprsdotfi_api_key=aprsdotfi_api_key,
        )
        if found_my_keyword or kw_err:
            found_my_duty_roster = found_my_keyword
            err = kw_err
            what = parser_rd_osm["what"]
            latitude = parser_rd_osm["latitude"]
            longitude = parser_rd_osm["longitude"]
            lasttime = parser_rd_osm["lasttime"]
            comment = parser_rd_osm["comment"]
            altitude = parser_rd_osm["altitude"]
            human_readable_message = parser_rd_osm["human_readable_message"]
            aprs_message = parser_rd_osm["aprs_message"]
            message_callsign = parser_rd_osm["message_callsign"]
            osm_special_phrase = parser_rd_osm["osm_special_phrase"]

    # Check for a keyword-based DAPNET message command
    if not found_my_duty_roster and not err:
        found_my_keyword, kw_err, parser_rd_dapnet = parse_what_keyword_dapnet(
            aprs_message=aprs_message, users_callsign=users_callsign
        )
        if found_my_keyword or kw_err:
            found_my_duty_roster = found_my_keyword
            err = kw_err
            what = parser_rd_dapnet["what"]
            message_callsign = parser_rd_dapnet["message_callsign"]
            human_readable_message = parser_rd_dapnet["human_readable_message"]
            aprs_message = parser_rd_dapnet["aprs_message"]
            dapnet_message = parser_rd_dapnet["dapnet_message"]

    # Check for a fortune cookie command
    if not found_my_duty_roster and not err:
        (
            found_my_keyword,
            kw_err,
            parser_rd_fortuneteller,
        ) = parse_what_keyword_fortuneteller(aprs_message=aprs_message)
        if found_my_keyword or kw_err:
            found_my_duty_roster = found_my_keyword
            err = kw_err
            what = parser_rd_fortuneteller["what"]
            aprs_message = parser_rd_fortuneteller["aprs_message"]

    # Check if the user wants to send his position data to
    # an email address as a position report
    if not found_my_duty_roster and not err:
        (
            found_my_keyword,
            kw_err,
            parser_rd_email_posrpt,
        ) = parse_what_keyword_email_position_report(
            aprs_message=aprs_message,
            users_callsign=users_callsign,
            aprsdotfi_api_key=aprsdotfi_api_key,
            language=language,
        )
        if found_my_keyword or kw_err:
            found_my_duty_roster = found_my_keyword
            err = kw_err
            latitude = parser_rd_email_posrpt["latitude"]
            longitude = parser_rd_email_posrpt["longitude"]
            users_latitude = parser_rd_email_posrpt["users_latitude"]
            users_longitude = parser_rd_email_posrpt["users_longitude"]
            lasttime = parser_rd_email_posrpt["lasttime"]
            comment = parser_rd_email_posrpt["comment"]
            altitude = parser_rd_email_posrpt["altitude"]
            what = parser_rd_email_posrpt["what"]
            human_readable_message = parser_rd_email_posrpt["human_readable_message"]
            aprs_message = parser_rd_email_posrpt["aprs_message"]
            message_callsign = parser_rd_email_posrpt["message_callsign"]
            city = parser_rd_email_posrpt["city"]
            state = parser_rd_email_posrpt["state"]
            county = parser_rd_email_posrpt["county"]
            country = parser_rd_email_posrpt["country"]
            country_code = parser_rd_email_posrpt["country_code"]
            district = parser_rd_email_posrpt["district"]
            address = parser_rd_email_posrpt["address"]
            zipcode = parser_rd_email_posrpt["zipcode"]
            street = parser_rd_email_posrpt["street"]
            street_number = parser_rd_email_posrpt["street_number"]
            mail_recipient = parser_rd_email_posrpt["mail_recipient"]

    # Check if the user has requested information wrt METAR data
    # potential inputs: ICAO/IATA qualifiers with/without keyword
    # and METAR keyword
    #
    # Similar to the Wx branch, this section also operates with keyword-less
    # parsing and needs to be placed relatively to the end of the input parser
    # in order to avoid misinterpretations wrt the message content.
    if not found_my_duty_roster and not err:
        (
            found_my_keyword,
            kw_err,
            parser_rd_metar,
        ) = parse_what_keyword_metar(
            aprs_message=aprs_message,
            users_callsign=users_callsign,
            aprsdotfi_api_key=aprsdotfi_api_key,
        )
        # did we find something? Then overwrite the existing variables with the retrieved content
        if found_my_keyword or kw_err:
            found_my_duty_roster = found_my_keyword
            err = kw_err
            what = parser_rd_metar["what"]
            message_callsign = parser_rd_metar["message_callsign"]
            icao = parser_rd_metar["icao"]
            human_readable_message = parser_rd_metar["human_readable_message"]
            aprs_message = parser_rd_metar["aprs_message"]

    # The parser process ends with wx-related keyword data, meaning
    # that the user has either specified a keyword-less address, a zip code
    # with keyword and (potentially) with country, a grid locator or
    # a set of lat/lon coordinates.
    #
    # IMPORTANT:
    #
    # This is the default/fallback branch which makes a LOT of guesstimates.
    # For example, if you simply send a call sign to MPAD, it will assume that
    # you want the wx for this call sign. Therefore, this parser process
    # has to be placed at the END of the parser - otherwise, there is a high
    # chance of misinterpreting the user's message

    if not found_my_duty_roster and not err:
        (
            found_my_keyword,
            kw_err,
            parser_rd_wx,
        ) = parse_what_keyword_wx(
            aprs_message=aprs_message, users_callsign=users_callsign, language=language
        )
        # did we find something? Then overwrite the existing variables with the retrieved content
        if found_my_keyword or kw_err:
            found_my_duty_roster = found_my_keyword
            err = kw_err
            latitude = parser_rd_wx["latitude"]
            longitude = parser_rd_wx["longitude"]
            what = parser_rd_wx["what"]
            message_callsign = parser_rd_wx["message_callsign"]
            human_readable_message = parser_rd_wx["human_readable_message"]
            aprs_message = parser_rd_wx["aprs_message"]
            city = parser_rd_wx["city"]
            state = parser_rd_wx["state"]
            country = parser_rd_wx["country"]
            country_code = parser_rd_wx["country_code"]
            district = parser_rd_wx["district"]
            address = parser_rd_wx["address"]
            zipcode = parser_rd_wx["zipcode"]
            county = parser_rd_wx["county"]
            street = parser_rd_wx["street"]
            street_number = parser_rd_wx["street_number"]

    # By now, we should know WHAT the user wants.
    #
    # Exception: user wants a wx report for his position
    # in this case, the 'what' keyword is still empty and is
    # implicitly defined via the 'when' keyword. In this particular
    # case, the use may send a simple e.g. 'tomorrow' request to us
    # and we set the 'wx' 'what' command implicitly.
    #
    # For all other cases, 'what' should now be set
    # Now let's try to figure out WHEN certain things are expected
    # for. We only enter the WHEN parser routines in case the
    # previous parser did nor encounter any errors.
    #

    #
    # We have reached the very end of the parser
    # Now check if we have received something useful
    #
    #
    # Check if we found ANYTHING valid at all
    if not what and not when and not when_daytime:
        # If the parser function has not returned an error message,
        # then set a default error message
        if not human_readable_message:
            human_readable_message = errmsg_invalid_command
        err = True

    # This is somewhat of a message garbage handler. Check
    # If we have received a 'when' or 'when_daytime' message
    # from the user. If we have received one AND 'what' is still
    # not set AND the remainder of the incoming APRS_message has
    # still content, then we were unable to digest parts of the
    # user's message. Rather than running the default wx command,
    # we return an error and tell the user to be more precise in
    # his commands.
    if not what:
        if when or when_daytime:
            if len(aprs_message) > 0:
                err = True
                human_readable_message = errmsg_invalid_command

    # Finally, apply the default settings in case the user
    # hasn't filled in all of the gaps
    #
    # Apply default to 'when' setting if still not populated
    if not found_when and not err:
        when = "today"
        found_when = True
        date_offset = 0

    # apply default to 'when_daytime' if still not populated
    # for special keywords such as the "metar" keyword, we
    # (ab)use the when_daytime field for other purposes, thus
    # allowing us to control the output in a proper way UNLESS
    # the user has explicitly specified the "full" command
    if not found_when_daytime and not err:
        if what in ("metar", "taf"):
            # populate with some default value so that we
            # don't run into trouble later on
            when_daytime = "day"
        else:
            when_daytime = "full"
        found_when_daytime = True

    # apply default to 'what' if still not populated
    if not what and not err:
        what = "wx"

    # Check if there is no reference to any position. This can be the case if
    # the user has requested something like 'tonight' where MPAD is to return
    # the weather for the user's call sign position. However, only do this if
    # the user has submitted a 'when' information (we don't care about the
    # 'when_daytime') as otherwise, garbage data will trigger a wx report

    if not found_my_duty_roster and not err:
        # the user has specified a time setting (e.g. 'today') so we know that
        # he actually wants us something to do (rather than just sending
        # random garbage data to us
        if when:
            # First, have a look at the user's complete call sign
            # including SSID
            (
                success,
                latitude,
                longitude,
                altitude,
                lasttime,
                comment,
                message_callsign,
            ) = get_position_on_aprsfi(
                aprsfi_callsign=users_callsign, aprsdotfi_api_key=aprsdotfi_api_key
            )
            if success:
                human_readable_message = f"{message_callsign}"
                found_my_duty_roster = True

                # (try) to translate into human readable information
                success, response_data = get_reverse_geopy_data(
                    latitude=latitude, longitude=longitude, language=language
                )
                if success:
                    # extract all fields as they will be used for the creation of the
                    # outgoing data dictionary
                    city = response_data["city"]
                    state = response_data["state"]
                    country = response_data["country"]
                    country_code = response_data["country_code"]
                    district = response_data["district"]
                    address = response_data["address"]
                    zipcode = response_data["zipcode"]
                    county = response_data["county"]
                    street = response_data["street"]
                    street_number = response_data["street_number"]
                    # build the HRM message based on the given data
                    human_readable_message = build_human_readable_address_message(
                        response_data
                    )
            else:
                # we haven't found anything? Let's get rid of the SSID and
                # give it one final try. If we still can't find anything,
                # then we will give up
                matches = re.search(
                    pattern=r"^(([A-Z0-9]{1,3}[0123456789][A-Z0-9]{0,3})-([A-Z0-9]{1,2}))$",
                    string=users_callsign,
                )
                if matches:
                    (
                        success,
                        latitude,
                        longitude,
                        altitude,
                        lasttime,
                        comment,
                        message_callsign,
                    ) = get_position_on_aprsfi(
                        aprsfi_callsign=matches[2].upper(),
                        aprsdotfi_api_key=aprsdotfi_api_key,
                    )
                    if success:
                        found_my_duty_roster = True
                        human_readable_message = f"{message_callsign}"
                        success, response_data = get_reverse_geopy_data(
                            latitude=latitude, longitude=longitude, language=language
                        )
                        if success:
                            # extract all fields as they will be used for the creation of the
                            # outgoing data dictionary
                            city = response_data["city"]
                            state = response_data["state"]
                            country = response_data["country"]
                            country_code = response_data["country_code"]
                            district = response_data["district"]
                            address = response_data["address"]
                            zipcode = response_data["zipcode"]
                            county = response_data["county"]
                            street = response_data["street"]
                            street_number = response_data["street_number"]
                            # build the HRM message based on the given data
                            human_readable_message = (
                                build_human_readable_address_message(response_data)
                            )
                    else:
                        human_readable_message = errmsg_cannot_find_coords_for_user
                        err = True
        else:
            if not human_readable_message:
                human_readable_message = errmsg_invalid_command
            err = True

    # Generate dictionary which contains what we have fund out about the user's request
    response_parameters = {
        "latitude": latitude,  # numeric latitude value
        "longitude": longitude,  # numeric longitude value
        "altitude": altitude,  # altitude; UOM is always 'meters'
        "lasttime": lasttime,  # last time the cs was heard on that given position
        "comment": comment,  # (potential) position comment from aprs.fi
        "when": when,  # day setting for 'when' command keyword
        "when_daytime": when_daytime,  # daytime setting for 'when' command keyword
        "what": what,  # contains the command that the user wants us to execute
        "units": units,  # units of measure, 'metric' or 'imperial'
        "message_callsign": message_callsign,  # This is the TARGET callsign which was either specified directly in the msg request or was assigned implicitly
        "users_callsign": users_callsign,  # user's call sign. This is the call sign that has sent us the message request
        "language": language,  # iso639-1 a2 message code
        "icao": icao,  # ICAO code
        "human_readable_message": human_readable_message,  # Message text header
        "date_offset": date_offset,  # precalculated date offset, based on 'when' value
        "hour_offset": hour_offset,  # precalculated hour offset, based on 'when' value
        "satellite": satellite,  # satellite name, e.g. 'ISS'
        "repeater_band": repeater_band,  # repeater band, e.g. '70cm'
        "repeater_mode": repeater_mode,  # repeater mode, e.g. 'c4fm'
        "city": city,  # address information
        "state": state,
        "country": country,
        "country_code": country_code,
        "county": county,
        "district": district,
        "address": address,
        "zipcode": zipcode,
        "cwop_id": cwop_id,
        "street": street,
        "street_number": street_number,
        "users_latitude": users_latitude,  # User's own lat / lon. Only used for 'whereis' request
        "users_longitude": users_longitude,  # in reference to another user's call sign
        "number_of_results": number_of_results,  # for keywords which may return more than 1 result
        "osm_special_phrase": osm_special_phrase,  # openstreetmap special phrases https://wiki.openstreetmap.org/wiki/Nominatim/Special_Phrases/EN
        "dapnet_message": dapnet_message,
        "mail_recipient": mail_recipient,  # APRS position reports that are sent to an email address
        "force_outgoing_unicode_messages": force_outgoing_unicode_messages,  # True if the user demands UTF8 MPAD messages
    }

    # Finally, set the return code. Unless there was an error, we return a True status
    # The 'human_readable_message' contains either the error text or the reference to
    # the data that the user has requested from us (the actual data such as the wx data
    # is retrieved in the next step.
    success = True
    if err:
        success = False

    return success, response_parameters


def parse_when(aprs_message: str):
    """
    Parse the 'when' information of the user's message
    (specific day or relative day such as 'tomorrow'
    Parameters
    ==========
    aprs_message : 'str'
        the original APRS message that we want to examine

    Returns
    =======
    aprs_message : 'str'
        the original APRS message minus some potential search hits
    found_when: 'bool'
        Current state of the 'when' parser. True if content has been found
    when: 'str'
        If we found some 'when' content, then its normalized content is
        returned with this variable
    date_offset: 'int'
        If we found some date-related 'when' content, then this
        field contains the tnteger offset in reference to the current day.
        Value between 0 (current day) and 7
    hour_offset: 'int'
        If we found some time-related 'when' content, then this
        field contains the integer offset in reference to the current
        hour value. Default value is -1. Only use this field's value if
        'when' value is 'hour'.
    """
    found_when = False
    when = None
    date_offset = hour_offset = -1

    regex_match = None

    regex_string = r"\b(tonite|tonight)\b"
    matches = re.findall(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        when = "today"
        found_when = True
        date_offset = 0
        regex_match = regex_string

    if not found_when:
        regex_string = r"\b(today)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when = "today"
            found_when = True
            date_offset = 0
            regex_match = regex_string

    if not found_when:
        regex_string = r"\b(tomorrow)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when = "tomorrow"
            found_when = True
            date_offset = 1
            regex_match = regex_string

    if not found_when:
        regex_string = r"\b(monday|mon)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when = "monday"
            found_when = True
            date_offset = getdaysuntil(calendar.MONDAY)
            regex_match = regex_string

    if not found_when:
        regex_string = r"\b(tuesday|tue)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when = "tuesday"
            found_when = True
            date_offset = getdaysuntil(calendar.TUESDAY)
            regex_match = regex_string

    if not found_when:
        regex_string = r"\b(wednesday|wed)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when = "wednesday"
            found_when = True
            date_offset = getdaysuntil(calendar.WEDNESDAY)
            regex_match = regex_string

    if not found_when:
        regex_string = r"\b(thursday|thu)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when = "thursday"
            found_when = True
            date_offset = getdaysuntil(calendar.THURSDAY)
            regex_match = regex_string

    if not found_when:
        regex_string = r"\b(friday|fri)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when = "friday"
            found_when = True
            date_offset = getdaysuntil(calendar.FRIDAY)
            regex_match = regex_string

    if not found_when:
        regex_string = r"\b(saturday|sat)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when = "saturday"
            found_when = True
            date_offset = getdaysuntil(calendar.SATURDAY)
            regex_match = regex_string

    if not found_when:
        regex_string = r"\b(sunday|sun)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when = "sunday"
            found_when = True
            date_offset = getdaysuntil(calendar.SUNDAY)
            regex_match = regex_string

    if not found_when:
        regex_string = r"\b(current|now)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when = "now"
            found_when = True
            date_offset = 0
            regex_match = regex_string

    if not found_when:
        regex_string = r"\b([1-7])d\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            try:
                date_offset = int(matches[0])
                regex_match = regex_string
                when = f"{date_offset}d"
                found_when = True
            except (ValueError, IndexError) as e:
                when = None
                found_when = False
                date_offset = -1

    # WX supports hourly wx forecasts for up to 47h, let's get that value
    if not found_when:
        regex_string = r"\b(4[0-7]|3[0-9]|2[0-9]|1[0-9]|[1-9])h\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when = "hour"
            found_when = True
            try:
                hour_offset = int(matches[0])
                regex_match = regex_string
            except (ValueError, IndexError) as e:
                when = None
                found_when = False
                hour_offset = -1

    # If we have found an entry AND have a matching regex,
    # then remove that string from the APRS message
    if found_when and regex_match:
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()

    return aprs_message, found_when, when, date_offset, hour_offset


def parse_when_daytime(aprs_message: str):
    """
    Parse the 'when_daytime' information of the user's message
    (can either be the 'full' day or something like 'night','morning')
    ==========
    aprs_message : 'str'
        the original APRS message that we want to examine

    Returns
    =======
    aprs_message : 'str'
        the original APRS message minus some potential search hits
    found_when_daytime: 'bool'
        Current state of the 'when_daytime' parser. True if content has been found
    when_daytime: 'str'
        If we found some 'when_daytime' content, then its normalized content is
        returned with this variable
    """
    found_when_daytime = False
    when_daytime = None

    regex_match = None

    regex_string = r"\b(full)\b"
    matches = re.findall(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        when_daytime = "full"
        found_when_daytime = True
        regex_match = regex_string

    if not when_daytime:
        regex_string = r"\b(morn|morning)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when_daytime = mpad_config.mpad_str_morning
            found_when_daytime = True
            regex_match = regex_string

    if not when_daytime:
        regex_string = r"\b(day|daytime|noon)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when_daytime = mpad_config.mpad_str_daytime
            found_when_daytime = True
            regex_match = regex_string

    if not when_daytime:
        regex_string = r"\b(eve|evening)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when_daytime = mpad_config.mpad_str_evening
            found_when_daytime = True
            regex_match = regex_string

    if not when_daytime:
        regex_string = r"\b(tonight|tonite|nite|night)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            when_daytime = mpad_config.mpad_str_night
            found_when_daytime = True
            regex_match = regex_string

    # If we have found an entry AND have a matching regex,
    # then remove that string from the APRS message
    if found_when_daytime and regex_match:
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()

    return aprs_message, found_when_daytime, when_daytime


def parse_what_keyword_repeater(
    aprs_message: str, users_callsign: str, aprsdotfi_api_key: str
):
    """
    Check if the user wants us to search for the nearest repeater
    this function always relates to the user's own call sign and not to
    foreign ones. The user can ask us for the nearest repeater in
    optional combination with band and/or mode (FM, C4FM, DSTAR et al)

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message
    users_callsign : 'str'
        Call sign of the user that has sent us the message
    aprsdotfi_api_key : 'str'
        aprs.fi access key

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    kw_err: 'bool'
        True if an error has occurred. If found_my_keyword is also true,
        then the error marker overrides the 'found' keyword
    parser_rd_repeater: 'dict'
        dictionary, containing the keyword-relevant data
    """
    # Search for repeater-mode-band
    what = repeater_band = repeater_mode = human_readable_message = comment = None
    lasttime = datetime.min
    latitude = longitude = 0.0
    altitude = 0
    message_callsign = users_callsign
    found_my_keyword = kw_err = False
    regex_string = (
        r"\brepeater\s*(fm|dstar|d-star|dmr|c4fm|ysf|tetra|atv)\s*(\d.?\d*(?:cm|m)\b)\b"
    )
    matches = re.search(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        repeater_mode = matches[1].upper().strip()
        repeater_band = matches[2].lower().strip()
        found_my_keyword = True
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()
    # If not found, search for repeater-band-mode
    if not found_my_keyword:
        regex_string = r"\brepeater\s*(\d.?\d*(?:cm|m)\b)\s*(fm|dstar|d-star|dmr|c4fm|ysf|tetra|atv)\b"
        matches = re.search(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            repeater_mode = matches[2].upper()
            repeater_band = matches[1].lower()
            found_my_keyword = True
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
    # if not found, search for repeater - mode
    if not found_my_keyword:
        regex_string = r"\brepeater\s*(fm|dstar|d-star|dmr|c4fm|ysf|tetra|atv)\b"
        matches = re.search(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            repeater_mode = matches[1].upper()
            repeater_band = None
            found_my_keyword = True
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
    # if not found, search for repeater-band
    if not found_my_keyword:
        regex_string = r"\brepeater\s*(\d.?\d*(?:cm|m)\b)\b"
        matches = re.search(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            repeater_band = matches[1].lower()
            repeater_mode = None
            found_my_keyword = True
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
    # If not found, just search for the repeater keyword
    if not found_my_keyword:
        regex_string = r"\brepeater\b"
        matches = re.search(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            repeater_band = None
            repeater_mode = None
            found_my_keyword = True
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
    if found_my_keyword:
        what = "repeater"
        human_readable_message = "Repeater"
        if repeater_band:
            human_readable_message += f" {repeater_band}"
        if repeater_mode:
            human_readable_message += f" {repeater_mode}"
        (
            success,
            latitude,
            longitude,
            altitude,
            lasttime,
            comment,
            message_callsign,
        ) = get_position_on_aprsfi(
            aprsfi_callsign=users_callsign, aprsdotfi_api_key=aprsdotfi_api_key
        )
        if not success:
            kw_err = True
            human_readable_message = (
                f"{errmsg_cannot_find_coords_for_user} {message_callsign}"
            )
    parser_rd_repeater = {
        "what": what,
        "latitude": latitude,
        "longitude": longitude,
        "altitude": altitude,
        "lasttime": lasttime,
        "comment": comment,
        "message_callsign": message_callsign,
        "repeater_band": repeater_band,
        "repeater_mode": repeater_mode,
        "human_readable_message": human_readable_message,
        "aprs_message": aprs_message,
    }
    return found_my_keyword, kw_err, parser_rd_repeater


def parse_what_keyword_metar(
    aprs_message: str, users_callsign: str, aprsdotfi_api_key: str
):
    """
    Keyword parser for the IATA/ICAO/METAR keywords (resulting in
    a request for METAR or TAF data for a specific airport)

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message
    users_callsign : 'str'
        Call sign of the user that has sent us the message
    aprsdotfi_api_key : 'str'
        aprs.fi access key

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    kw_err: 'bool'
        True if an error has occurred. If found_my_keyword is also true,
        then the error marker overrides the 'found' keyword
    parser_rd_metar: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    # Error flag is not used; we keep it for output parameter
    # consistency reasons with the other keyword parsers
    found_my_keyword = kw_err = False
    human_readable_message = what = icao = None

    # Check if the user has requested information wrt a 4-character ICAO code
    # if we can find the code, then check if the airport is METAR-capable. If
    # that is not the case, then return the do not request METAR data but a
    # regular wx report
    #
    regex_string = r"\b(icao)\s*([a-zA-Z0-9]{4})\b"
    matches = re.findall(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        (_, icao) = matches[0]
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()
        # try to look up the airport coordinates based on the ICAO code
        success, latitude, longitude, metar_capable, icao = validate_icao(
            icao_code=icao
        )
        if success:
            what = "metar"
            found_my_keyword = True
            human_readable_message = f"METAR for '{icao}'"
            # If we did find the airport but it is not METAR-capable,
            # then provide a wx report instead
            if not metar_capable:
                what = "wx"
                human_readable_message = f"Wx for '{icao}'"
        else:
            # the user has explicitly requested an ICAO code which seems to be invalid
            # Therefore, flag this as error and return the message back to the user
            human_readable_message = f"Cannot locate airport ICAO code {icao}"
            icao = None
            kw_err = True

    # Check if the user has requested information wrt a 3-character IATA code
    # if we can find the code, then check if the airport is METAR-capable. If
    # that is not the case, then return the do not request METAR data but a
    # regular wx report
    #
    if not found_my_keyword and not kw_err:
        regex_string = r"\b(iata)\s*([a-zA-Z0-9]{3})\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            (_, iata) = matches[0]
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
            # try to look up the airport coordinates based on the IATA code
            success, latitude, longitude, metar_capable, icao = validate_iata(
                iata_code=iata
            )
            if success:
                what = "metar"
                found_my_keyword = True
                human_readable_message = f"METAR for '{icao}'"
                # If we did find the airport but it is not METAR-capable,
                # then provide a wx report instead
                if not metar_capable:
                    what = "wx"
                    human_readable_message = f"Wx for '{icao}'"
            else:
                # the user has explicitly requested an IATA code which seems to be invalid
                # Therefore, flag this as error and return the message back to the user
                human_readable_message = f"Cannot locate airport IATA code {iata}"
                icao = None
                kw_err = True

    # Check for a keyword-less ICAO code
    if not found_my_keyword and not kw_err:
        # This is a (sometimes) futile attempt to distinguish any keyword-less
        # wx data requests from keyword-less METAR requests. If the APRS
        # message contains ";" or ",", then we assume that the request is
        # wx-related and do not process it any further
        #
        # Without this fix, a wx request for e.g. "Bad Driburg;de" would not
        # result in wx data for the German city of Bad Driburg but for a METAR
        # report for ICAO code KBAD / IATA code BAD
        if "," not in aprs_message and ";" not in aprs_message:
            regex_string = r"\b([a-zA-Z0-9]{4})\b"
            matches = re.findall(
                pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
            )
            if matches:
                # Check if what we found is a potential and existing ICAO code
                # it CAN be something else - so we need to check first
                success, latitude, longitude, metar_capable, icao = validate_icao(
                    icao_code=matches[0].strip()
                )
                if success:
                    # Yes, we have verified this as a valid ICAO code
                    what = "metar"
                    found_my_keyword = True
                    human_readable_message = f"METAR for '{icao}'"
                    aprs_message = re.sub(
                        pattern=regex_string,
                        repl="",
                        string=aprs_message,
                        flags=re.IGNORECASE,
                    ).strip()
                    # If we did find the airport but it is not METAR-capable,
                    # then supply a wx report instead
                    if not metar_capable:
                        what = "wx"
                        human_readable_message = f"Wx for '{icao}'"

    # Check for a keyword-less IATA code
    if not found_my_keyword and not kw_err:
        # This is a (sometimes) futile attempt to distinguish any keyword-less
        # wx data requests from keyword-less METAR requests. If the APRS
        # message contains ";" or ",", then we assume that the request is
        # wx-related and do not process it any further
        #
        # Without this fix, a wx request for e.g. "Bad Driburg;de" would not
        # result in wx data for the German city of Bad Driburg but for a METAR
        # report for ICAO code KBAD / IATA code BAD
        if "," not in aprs_message and ";" not in aprs_message:
            regex_string = r"\b([a-zA-Z0-9]{3})\b"
            matches = re.findall(
                pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
            )
            if matches:
                # Check if what we found is a potential and existing IATA code
                # it CAN be something else - so we need to check first
                success, latitude, longitude, metar_capable, icao = validate_iata(
                    iata_code=matches[0].strip()
                )
                if success:
                    # Yes, we have verified this as a valid IATA code and have received the
                    # corresponding ICAO code
                    what = "metar"
                    found_my_keyword = True
                    human_readable_message = f"METAR for '{icao}'"
                    aprs_message = re.sub(
                        pattern=regex_string,
                        repl="",
                        string=aprs_message,
                        flags=re.IGNORECASE,
                    ).strip()
                    # If we did find the airport but it is not METAR-capable,
                    # then supply a wx report instead
                    if not metar_capable:
                        what = "wx"
                        human_readable_message = f"Wx for '{icao}'"

    # if the user has specified the 'metar' keyword, then
    # try to determine the nearest airport in relation to
    # the user's own call sign position
    if not found_my_keyword and not kw_err:
        regex_string = r"\b(metar)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            (
                success,
                latitude,
                longitude,
                altitude,
                lasttime,
                comment,
                message_callsign,
            ) = get_position_on_aprsfi(
                aprsfi_callsign=users_callsign, aprsdotfi_api_key=aprsdotfi_api_key
            )
            if success:
                icao = get_nearest_icao(latitude=latitude, longitude=longitude)
                if icao:
                    (
                        success,
                        latitude,
                        longitude,
                        metar_capable,
                        icao,
                    ) = validate_icao(icao)
                    if success:
                        what = "metar"
                        human_readable_message = f"METAR for '{icao}'"
                        found_my_keyword = True
                        aprs_message = re.sub(
                            pattern=regex_string,
                            repl="",
                            string=aprs_message,
                            flags=re.IGNORECASE,
                        ).strip()
                        # If we did find the airport but it is not METAR-capable,
                        # then supply a wx report instead
                        if not metar_capable:
                            what = "wx"
                            icao = None
                            human_readable_message = f"Wx for '{icao}'"

    # if the user has specified the 'taf' keyword, then
    # try to determine the nearest airport in relation to
    # the user's own call sign position
    if not found_my_keyword and not kw_err:
        regex_string = r"\b(taf)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            (
                success,
                latitude,
                longitude,
                altitude,
                lasttime,
                comment,
                message_callsign,
            ) = get_position_on_aprsfi(
                aprsfi_callsign=users_callsign, aprsdotfi_api_key=aprsdotfi_api_key
            )
            if success:
                icao = get_nearest_icao(latitude=latitude, longitude=longitude)
                if icao:
                    (
                        success,
                        latitude,
                        longitude,
                        metar_capable,
                        icao,
                    ) = validate_icao(icao)
                    if success:
                        what = "taf"
                        human_readable_message = f"TAF for '{icao}'"
                        found_my_keyword = True
                        aprs_message = re.sub(
                            pattern=regex_string,
                            repl="",
                            string=aprs_message,
                            flags=re.IGNORECASE,
                        ).strip()
                        # If we did find the airport but it is not METAR-capable,
                        # then supply a wx report instead
                        if not metar_capable:
                            what = "wx"
                            icao = None
                            human_readable_message = f"Wx for '{icao}'"
    parser_rd_metar = {
        "what": what,
        "message_callsign": users_callsign,
        "human_readable_message": human_readable_message,
        "aprs_message": aprs_message,
        "icao": icao,
    }

    return found_my_keyword, kw_err, parser_rd_metar


def parse_what_keyword_wx(aprs_message: str, users_callsign: str, language: str):
    """
    wx-Keyword-less default parser for WX-related data:
    - address data (city/state/country) (not using any keywords)
    - zip code (using keywords)
    - lat/lon (not using any keywords)
    - maidenhead (using keywords)

    wx-keyword-less does not mean that there aren't any keywords -
    it just means that there is not the 'wx' keyword which relates only
    to a user's call sign. Welcome to the wonderful world of providing
    the best experience to your users :-)

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message
    users_callsign : 'str'
        Call sign of the user that has sent us the message
    language : 'str'
        iso639-2 language code

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    kw_err: 'bool'
        True if an error has occurred. If found_my_keyword is also true,
        then the error marker overrides the 'found' keyword
    parser_rd_wx: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    found_my_keyword = kw_err = success = False
    human_readable_message = what = None
    latitude = longitude = 0.0

    what = city = state = country = country_code = district = address = zipcode = None
    street = street_number = county = None

    # By default, we assume that the callsign that is in relevance to
    # the wx data is our own call sign. However, this setting can be
    # overwritten if the user requests wx data for a different
    # user's position
    message_callsign = users_callsign

    # Now let's start with examining the message text.
    # Rule of thumb:
    # 1) the first successful match will prevent
    # parsing of *location*-related information
    # 2) If we find some data in this context, then it will
    # be removed from the original message in order to avoid
    # any additional occurrences at a later point in time.

    # Check if we have been given a specific address (city, state, country code)
    geopy_query = None
    # City / State / Country?
    regex_string = r"\b([\D\s]+),\s*?(\w+);\s*([a-zA-Z]{2})\b"
    matches = re.findall(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        (city, state, country_code) = matches[0]
        city = string.capwords(city).strip()
        country_code = country_code.upper().strip()
        state = state.upper().strip()  # in theory, this could also be a non-US state
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()
        geopy_query = {"city": city, "state": state, "country": country_code}
        found_my_keyword = True
    # City / State
    if not found_my_keyword and not kw_err:
        regex_string = r"\b([\D\s]+),\s*?(\w+)\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            (city, state) = matches[0]
            country_code = "US"
            country = "United States"
            city = string.capwords(city).strip()
            state = state.upper().strip()
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
            geopy_query = {"city": city, "state": state, "country": country_code}
            found_my_keyword = True
    # City / Country Code
    if not found_my_keyword and not kw_err:
        regex_string = r"\b([\D\s]+);\s*([a-zA-Z]{2})\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            (city, country_code) = matches[0]
            city = string.capwords(city).strip()
            country_code = country_code.upper().strip()
            state = None
            geopy_query = {"city": city, "country": country_code}
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
            found_my_keyword = True
    # Did I find something at all?
    # Yes; send the query to GeoPy and get lat/lon for the address
    if found_my_keyword and not kw_err:
        # Let's validate the given iso3166 country code
        if not validate_country(country_code):
            human_readable_message = f"{errmsg_invalid_country}: '{country_code}'"
            kw_err = True
        # Everything seems to be ok. Try to retrieve
        # lat/lon for the given data
        if not kw_err:
            success, latitude, longitude = get_geocode_geopy_data(
                query_data=geopy_query, language=language
            )
            if success:
                what = "wx"  # We know now that we want a wx report
                human_readable_message = city
                if state and country_code == "US":
                    human_readable_message += f",{state}"
                if country_code and country_code != "US":
                    human_readable_message += f";{country_code}"
            else:
                kw_err = True
                human_readable_message = errmsg_cannot_find_coords_for_address

    # Look for postal/zip code information
    # First, let's look for an international zip code
    # Format: zip[zipcode];[country code]
    # Country Code = iso3166-2
    if not found_my_keyword and not kw_err:
        geopy_query = None
        regex_string = r"\b(zip)\s*([a-zA-Z0-9-( )]{3,10});\s*([a-zA-Z]{2})\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            (_, zipcode, country_code) = matches[0]
            zipcode = zipcode.strip()
            state = None
            country_code = country_code.upper().strip()
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
            found_my_keyword = True
            # prepare the geopy reverse lookup string
            geopy_query = {"postalcode": zipcode, "country": country_code}
        if not found_my_keyword:
            # check for a 5-digit zip code with keyword
            # If match: assume that the user wants a US zip code
            regex_string = r"\b(zip)\s*([0-9]{5})\b"
            matches = re.findall(
                pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
            )
            if matches:
                (_, zipcode) = matches[0]
                state = None
                country_code = "US"
                country = "United States"
                zipcode = zipcode.strip()
                aprs_message = re.sub(
                    pattern=regex_string,
                    repl="",
                    string=aprs_message,
                    flags=re.IGNORECASE,
                ).strip()
                found_my_keyword = True
                # prepare the geopy reverse lookup string
                geopy_query = {"postalcode": zipcode, "country": country_code}
        if not found_my_keyword:
            # check if the user has submitted a 3-10 digit zipcode WITH
            # country code but WITHOUT keyword
            geopy_query = None
            regex_string = r"\b([a-zA-Z0-9-( )]{3,10});\s*([a-zA-Z]{2})\b"
            matches = re.findall(
                pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
            )
            if matches:
                (zipcode, country_code) = matches[0]
                zipcode = zipcode.strip()
                state = None
                country_code = country_code.upper().strip()
                aprs_message = re.sub(
                    pattern=regex_string,
                    repl="",
                    string=aprs_message,
                    flags=re.IGNORECASE,
                ).strip()
                found_my_keyword = True
                # prepare the geopy reverse lookup string
                geopy_query = {"postalcode": zipcode, "country": country_code}
        # Did I find something at all?
        # Yes; send the query to GeoPy and get lat/lon for the address
        if found_my_keyword:
            # First, let's validate the given iso3166 country code
            if not validate_country(country_code):
                human_readable_message = f"{errmsg_invalid_country}: '{country_code}'"
                kw_err = True
                what = None
            else:
                # Perform a reverse lookup. Query string was already pre-prepared.
                success, latitude, longitude = get_geocode_geopy_data(
                    query_data=geopy_query, language=language
                )
                if success:
                    # We only need latitude/longitude in order to get the wx report
                    # Therefore, we can already set the 'what' command keyword'
                    what = "wx"
                    # Pre-build the output message
                    human_readable_message = f"Zip {zipcode};{country_code}"
                    # but try to get a real city name
                    success, response_data = get_reverse_geopy_data(
                        latitude=latitude, longitude=longitude, language=language
                    )
                    if success:
                        # extract all fields as they will be used for the creation of the
                        # outgoing data dictionary
                        city = response_data["city"]
                        state = response_data["state"]
                        country_code = response_data["country_code"]
                        country = response_data["country"]
                        # zipcode = response_data["zipcode"]
                        county = response_data["county"]
                        street = response_data["street"]
                        street_number = response_data["street_number"]
                        # build the HRM message based on the given data
                        human_readable_message = build_human_readable_address_message(
                            response_data
                        )
                else:
                    kw_err = True
                    human_readable_message = errmsg_cannot_find_coords_for_address

    # Look for a single 5-digit code WITHOUT any additional qualifying information
    # if found then assume that it is a zip code from the US
    # and set all other variables accordingly
    # This approach honors wxbot's way of accessing zip codes. Other countries
    # such as DE also use zip codes of the same length but let's assume that
    # 5 digit zip codes are US only.
    if not found_my_keyword and not kw_err:
        regex_string = r"\b([0-9]{5})\b"
        matches = re.findall(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            zipcode = matches[0]
            state = None
            country_code = "US"
            country = "United States"
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
            found_my_keyword = True
            what = "wx"
            human_readable_message = f"Zip {zipcode};{country_code}"
            success, latitude, longitude = get_geocode_geopy_data(
                query_data={"postalcode": zipcode, "country": country_code},
                language=language,
            )
            if not success:
                kw_err = True
                human_readable_message = errmsg_cannot_find_coords_for_address
            else:
                # Finally, try to get a real city name
                success, response_data = get_reverse_geopy_data(
                    latitude=latitude, longitude=longitude, language=language
                )
                if success:
                    # extract all fields as they will be used for the creation of the
                    # outgoing data dictionary
                    city = response_data["city"]
                    state = response_data["state"]
                    country_code = response_data["country_code"]
                    country = response_data["country"]
                    district = response_data["district"]
                    address = response_data["address"]
                    zipcode = response_data["zipcode"]
                    county = response_data["county"]
                    street = response_data["street"]
                    street_number = response_data["street_number"]
                    # build the HRM message based on the given data
                    human_readable_message = build_human_readable_address_message(
                        response_data
                    )

    # check if the user has requested a set of maidenhead coordinates
    # Can either be 4- or 6-character set of maidenhead coordinates
    # if found, then transform to latitude/longitude coordinates
    # and remember that the user did specify maidenhead data, henceforth
    # we will not try to convert the coordinates to an actual
    # human-readable address
    if not found_my_keyword and not kw_err:
        regex_string = r"\b(grid|mh)\s*([a-zA-Z]{2}[0-9]{2}([a-zA-Z]{2})?)\b"
        matches = re.search(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            (latitude, longitude) = maidenhead.to_location(matches[2].strip())
            found_my_keyword = True
            human_readable_message = f"{matches[2]}"
            what = "wx"
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()

    # Not run another parser attempt on a keyword-less grid locator
    if not found_my_keyword and not kw_err:
        regex_string = r"\b[a-zA-Z]{2}[0-9]{2}([a-zA-Z]{2})?\b"
        matches = re.search(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            (latitude, longitude) = maidenhead.to_location(matches[0].strip())
            found_my_keyword = True
            human_readable_message = f"{matches[0]}"
            what = "wx"
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()

    # Check if the user has specified lat/lon information
    if not found_my_keyword and not kw_err:
        regex_string = r"\b([\d\.,\-]+)\/([\d\.,\-]+)\b"
        matches = re.search(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            success = True
            try:
                latitude = float(matches[1])
                longitude = float(matches[2])
            except (ValueError, IndexError) as e:
                latitude = longitude = 0
                success = False
            if success:
                # try to get human-readable coordinates
                success, response_data = get_reverse_geopy_data(
                    latitude=latitude, longitude=longitude, language=language
                )
                if success:
                    # extract all fields as they will be used for the creation of the
                    # outgoing data dictionary
                    city = response_data["city"]
                    state = response_data["state"]
                    country_code = response_data["country_code"]
                    country = response_data["country"]
                    district = response_data["district"]
                    address = response_data["address"]
                    zipcode = response_data["zipcode"]
                    county = response_data["county"]
                    street = response_data["street"]
                    street_number = response_data["street_number"]
                    # build the HRM message based on the given data
                    human_readable_message = build_human_readable_address_message(
                        response_data
                    )
                else:
                    # We didn't find anything; use the original input for the HRM
                    human_readable_message = f"lat {latitude}/lon {longitude}"
                aprs_message = re.sub(
                    pattern=regex_string,
                    repl="",
                    string=aprs_message,
                    flags=re.IGNORECASE,
                ).strip()
                found_my_keyword = True
                what = "wx"
            else:
                human_readable_message = "Error while parsing coordinates"
                kw_err = True

    # Look for a call sign either with or without SSID
    # note: in 99% of all cases, a single call sign means that
    # the user wants to get wx data - but we are not going to
    # assign the 'what' info for now and just extract the call sign
    if not found_my_keyword and not kw_err:
        regex_string = r"\b([a-zA-Z0-9]{1,3}[0-9][a-zA-Z0-9]{0,3}-[0-9]{1,2})\b"
        matches = re.search(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            message_callsign = matches[0].upper()
            found_my_keyword = True
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
        else:
            regex_string = r"\b([a-zA-Z0-9]{1,3}[0-9][a-zA-Z0-9]{0,3})\b"
            matches = re.search(
                pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
            )
            if matches:
                message_callsign = matches[0].upper()
                aprs_message = re.sub(
                    pattern=regex_string,
                    repl="",
                    string=aprs_message,
                    flags=re.IGNORECASE,
                ).strip()
                found_my_keyword = True
        if found_my_keyword and message_callsign:
            (
                success,
                latitude,
                longitude,
                altitude,
                lasttime,
                comment,
                message_callsign,
            ) = get_position_on_aprsfi(
                aprsfi_callsign=message_callsign,
                aprsdotfi_api_key=aprsdotfi_api_key,
            )
            if not success:
                human_readable_message = (
                    f"{errmsg_cannot_find_coords_for_user} {message_callsign}"
                )
                kw_err = True
            else:
                # Prepopulate our message to the user with a default
                human_readable_message = message_callsign
                what = "wx"
                # now try to build a human readable message
                success, response_data = get_reverse_geopy_data(
                    latitude=latitude, longitude=longitude, language=language
                )
                if success:
                    # extract all fields as they will be used for the creation of the
                    # outgoing data dictionary
                    city = response_data["city"]
                    state = response_data["state"]
                    country_code = response_data["country_code"]
                    country = response_data["country"]
                    district = response_data["district"]
                    address = response_data["address"]
                    zipcode = response_data["zipcode"]
                    county = response_data["county"]
                    street = response_data["street"]
                    street_number = response_data["street_number"]
                    # build the HRM message based on the given data
                    human_readable_message = build_human_readable_address_message(
                        response_data
                    )

    parser_rd_wx = {
        "latitude": latitude,
        "longitude": longitude,
        "what": what,
        "message_callsign": message_callsign,
        "human_readable_message": human_readable_message,
        "aprs_message": aprs_message,
        "city": city,
        "state": state,
        "country": country,
        "country_code": country_code,
        "district": district,
        "address": address,
        "zipcode": zipcode,
        "county": county,
        "street": street,
        "street_number": street_number,
    }

    return found_my_keyword, kw_err, parser_rd_wx


def parse_what_keyword_osm_category(
    aprs_message: str, users_callsign: str, aprsdotfi_api_key: str
):
    """
    Keyword parser for OpenStreetMap categories

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message
    users_callsign : 'str'
        Call sign of the user that has sent us the message
    aprsdotfi_api_key : 'str'
        aprs.fi access key

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    kw_err: 'bool'
        True if an error has occurred. If found_my_keyword is also true,
        then the error marker overrides the 'found' keyword
    parser_rd_osm: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    found_my_keyword = kw_err = success = False
    human_readable_message = what = osm_special_phrase = comment = None
    latitude = longitude = 0.0
    altitude = 0
    lasttime = datetime.min
    what = message_callsign = None

    for osm_category in mpad_config.osm_supported_keyword_categories:
        regex_string = rf"\bosm\s*({osm_category})\b"
        matches = re.search(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            osm_special_phrase = osm_category
            found_my_keyword = True
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
        if not found_my_keyword:
            regex_string = rf"\b({osm_category})\b"
            matches = re.search(
                pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
            )
            if matches:
                osm_special_phrase = osm_category
                found_my_keyword = True
                aprs_message = re.sub(
                    pattern=regex_string,
                    repl="",
                    string=aprs_message,
                    flags=re.IGNORECASE,
                ).strip()
        if found_my_keyword:
            what = "osm_special_phrase"
            (
                success,
                latitude,
                longitude,
                altitude,
                lasttime,
                comment,
                message_callsign,
            ) = get_position_on_aprsfi(
                aprsfi_callsign=users_callsign,
                aprsdotfi_api_key=aprsdotfi_api_key,
            )
            if not success:
                kw_err = True
                human_readable_message = (
                    f"{errmsg_cannot_find_coords_for_user} {message_callsign}"
                )
            break

    parser_rd_osm = {
        "latitude": latitude,
        "longitude": longitude,
        "lasttime": lasttime,
        "comment": comment,
        "altitude": altitude,
        "what": what,
        "human_readable_message": human_readable_message,
        "aprs_message": aprs_message,
        "message_callsign": message_callsign,
        "osm_special_phrase": osm_special_phrase,
    }
    return found_my_keyword, kw_err, parser_rd_osm


def parse_what_keyword_satpass(
    aprs_message: str, users_callsign: str, aprsdotfi_api_key: str
):
    """
    Keyword parser for OpenStreetMap categories

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message
    users_callsign : 'str'
        Call sign of the user that has sent us the message
    aprsdotfi_api_key : 'str'
        aprs.fi access key

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    kw_err: 'bool'
        True if an error has occurred. If found_my_keyword is also true,
        then the error marker overrides the 'found' keyword
    parser_rd_satpass: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    found_my_keyword = kw_err = success = False
    human_readable_message = what = comment = satellite = None
    latitude = longitude = 0.0
    altitude = 0
    lasttime = datetime.min

    what = message_callsign = None

    regex_string = r"\b(vispass|satpass|satfreq)\s*(\w*(\S*))\b"
    matches = re.search(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        # we deliberately accept ZERO..n characters for the satellite as the
        # user may have specified the keyword without any actual satellite
        # name. If that is the case, return an error to the user
        # (this is to prevent the user from receiving a wx report instead -
        # wx would kick in as default)
        _what_tmp = matches[1].lower()
        satellite = matches[2].strip().upper()
        if len(satellite) == 0:
            human_readable_message = errmsg_no_satellite_specified
            kw_err = True
        if not kw_err:
            (
                success,
                latitude,
                longitude,
                altitude,
                lasttime,
                comment,
                message_callsign,
            ) = get_position_on_aprsfi(
                aprsfi_callsign=users_callsign, aprsdotfi_api_key=aprsdotfi_api_key
            )
            if success:
                what = _what_tmp
                if what == "satfreq":
                    human_readable_message = f"SatFreq for {satellite}"
                else:
                    human_readable_message = f"SatPass for {satellite}"
                found_my_keyword = True
                aprs_message = re.sub(
                    pattern=regex_string,
                    repl="",
                    string=aprs_message,
                    flags=re.IGNORECASE,
                ).strip()
            else:
                human_readable_message = (
                    f"{errmsg_cannot_find_coords_for_user} {users_callsign}"
                )
                kw_err = True
    parser_rd_satpass = {
        "what": what,
        "latitude": latitude,
        "longitude": longitude,
        "altitude": altitude,
        "lasttime": lasttime,
        "comment": comment,
        "message_callsign": message_callsign,
        "satellite": satellite,
        "human_readable_message": human_readable_message,
        "aprs_message": aprs_message,
    }
    return found_my_keyword, kw_err, parser_rd_satpass


def parse_what_keyword_dapnet(aprs_message: str, users_callsign: str):
    """
    Keyword parser for DAPNET messaging. Supports 'dapnet' and
    'dapnethp' keywords (the latter sends out messages to DAPNET
    with high priority)

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message
    users_callsign : 'str'
        Call sign of the user that has sent us the message

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    kw_err: 'bool'
        True if an error has occurred. If found_my_keyword is also true,
        then the error marker overrides the 'found' keyword
    parser_rd_dapnet: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    found_my_keyword = kw_err = False
    human_readable_message = dapnet_message = None
    what = message_callsign = None

    regex_string = r"\b(dapnet|dapnethp)\s*([a-zA-Z0-9]{1,3}[0-9][a-zA-Z0-9]{0,3}-[a-zA-Z0-9]{1,2})\s*([\D\s]+)"
    matches = re.search(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        what = matches[1].lower()
        message_callsign = matches[2].upper().strip()
        dapnet_message = matches[3].strip()
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()
        found_my_keyword = True
    if not found_my_keyword:
        regex_string = (
            r"\b(dapnet|dapnethp)\s*([a-zA-Z0-9]{1,3}[0-9][a-zA-Z0-9]{0,3})\s*([\D\s]+)"
        )
        matches = re.search(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            what = matches[1].lower()
            message_callsign = matches[2].upper().strip()
            dapnet_message = matches[3].strip()
            found_my_keyword = True
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()

    parser_rd_dapnet = {
        "what": what,
        "message_callsign": message_callsign,
        "human_readable_message": human_readable_message,
        "aprs_message": aprs_message,
        "dapnet_message": dapnet_message,
    }
    return found_my_keyword, kw_err, parser_rd_dapnet


def parse_what_keyword_cwop_id(aprs_message: str, users_callsign: str):
    """
    Keyword parser for a user-specified CWOP station

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message
    users_callsign : 'str'
        Call sign of the user that has sent us the message

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    kw_err: 'bool'
        True if an error has occurred. If found_my_keyword is also true,
        then the error marker overrides the 'found' keyword
    parser_rd_cwop_id: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    found_my_keyword = kw_err = False
    human_readable_message = cwop_id = None
    what = None

    # Check if the user wants information about a specific CWOP ID
    regex_string = r"\bcwop\s*(\w+)\b"
    matches = re.search(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        cwop_id = matches[1].upper().strip()
        if len(cwop_id) == 0:
            human_readable_message = errmsg_no_cwop_specified
            kw_err = True
        else:
            what = "cwop_by_cwop_id"
            human_readable_message = f"CWOP for {cwop_id}"
            found_my_keyword = True
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()

    parser_rd_cwop_id = {
        "what": what,
        "message_callsign": users_callsign,
        "human_readable_message": human_readable_message,
        "aprs_message": aprs_message,
        "cwop_id": cwop_id,
    }
    return found_my_keyword, kw_err, parser_rd_cwop_id


def parse_what_keyword_callsign_multi(
    aprs_message: str, users_callsign: str, aprsdotfi_api_key: str, language: str = "en"
):
    """
    Multi-keyword parser in reference to a call sign
    which can either be the user's call sign or one that is
    embedded within the user's request

    Check if the user wants one of the following info
    for a specific call sign WITH or withOUT SSID:
    wx (Weather report for the user's position)
    whereis (location information for the user's position)
    riseset (Sunrise/Sunset and moonrise/moonset info)
    metar (nearest METAR data for the user's position)
    taf (nearest taf data for the user's position)
    CWOP (nearest CWOP data for user's position)

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message
    users_callsign : 'str'
        Call sign of the user that has sent us the message
    aprsdotfi_api_key : 'str'
        aprs.fi access key
    language : 'str'
        ISO639-a2 language

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    kw_err: 'bool'
        True if an error has occurred. If found_my_keyword is also true,
        then the error marker overrides the 'found' keyword
    parser_rd_csm: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    found_my_keyword = kw_err = success = False
    human_readable_message = what = icao = comment = None
    latitude = longitude = users_latitude = users_longitude = 0.0
    altitude = 0
    lasttime = datetime.min
    what = message_callsign = city = state = county = None
    zipcode = country = country_code = district = address = street = street_number = (
        None
    )

    # First check the APRS message and see if the user has submitted
    # a call sign with the message (we will first check for a call
    # sign with SSID, followed by a check for the call sign without
    # SSID). If no SSID was found, then just check for the command
    # sequence and -if found- use the user's call sign
    #
    # Check - full call sign with SSID
    regex_string = r"\b(wx|forecast|whereis|riseset|cwop|metar|taf|sonde)\s*([a-zA-Z0-9]{1,3}[0-9][a-zA-Z0-9]{0,3}-[a-zA-Z0-9]{1,2})\b"
    matches = re.search(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        what = matches[1].lower()
        message_callsign = matches[2].upper()
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()
        found_my_keyword = True
    if not found_my_keyword:
        # Check - call sign without SSID
        regex_string = r"\b(wx|forecast|whereis|riseset|cwop|metar|taf|sonde)\s*([a-zA-Z0-9]{1,3}[0-9][a-zA-Z0-9]{0,3})\b"
        matches = re.search(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            what = matches[1].lower()
            message_callsign = matches[2].upper()
            found_my_keyword = True
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
    if not found_my_keyword:
        # Check - call sign whose pattern deviates from the standard call sign pattern (e.g. bot, CWOP station etc)
        regex_string = r"\b(wx|forecast|whereis|riseset|cwop|metar|taf|sonde)\s*(\w+)\b"
        matches = re.search(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            what = matches[1].lower()
            message_callsign = matches[2].upper().strip()
            found_my_keyword = True
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
    if not found_my_keyword:
        # Check - no call sign at all. In this case, we use the sender's call sign as reference
        #
        # Hint: normally excludes the 'sonde' keyword as it requires a separate ID
        # But maybe the probe itself is asking for pos data so let's keep it in
        # Future processing of probe data will fail anyway if it's no radiosonde callsign
        regex_string = r"\b(wx|forecast|whereis|riseset|cwop|metar|taf|sonde)\b"
        matches = re.search(
            pattern=regex_string, string=aprs_message, flags=re.IGNORECASE
        )
        if matches:
            what = matches[1].lower()
            message_callsign = users_callsign
            found_my_keyword = True
            aprs_message = re.sub(
                pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
            ).strip()
    if found_my_keyword:
        (
            success,
            latitude,
            longitude,
            altitude,
            lasttime,
            comment,
            message_callsign,
        ) = get_position_on_aprsfi(
            aprsfi_callsign=message_callsign, aprsdotfi_api_key=aprsdotfi_api_key
        )
        if success:
            if what == "wx" or what == "forecast":
                human_readable_message = f"Wx {message_callsign}"
                if what == "forecast":
                    what = "wx"
            elif what == "riseset":
                human_readable_message = f"RiseSet {message_callsign}"
            elif what == "sonde":
                human_readable_message = f"Landing Pred. '{message_callsign}'"
                # Fetch the *sender's* lat/lon so that we can
                # calculate the distance between the sender's position
                # and the call sign that he has requested. We are only
                # interested in the user's lat/lon info
                (
                    success,
                    users_latitude,
                    users_longitude,
                    _,
                    _,
                    _,
                    _,
                ) = get_position_on_aprsfi(
                    aprsfi_callsign=users_callsign,
                    aprsdotfi_api_key=aprsdotfi_api_key,
                )
            elif what == "whereis":
                human_readable_message = f"Pos {message_callsign}"
                # Try to get the msg call sign's human readable address based on lat/lon
                # we ignore any errors as all output fields will be properly initialized with default values
                success, response_data = get_reverse_geopy_data(
                    latitude=latitude, longitude=longitude, language=language
                )
                # extract all fields as they will be used for the creation of the
                # outgoing data dictionary
                city = response_data["city"]
                state = response_data["state"]
                country_code = response_data["country_code"]
                country = response_data["country"]
                district = response_data["district"]
                address = response_data["address"]
                zipcode = response_data["zipcode"]
                county = response_data["county"]
                street = response_data["street"]
                street_number = response_data["street_number"]

                # ultimately, get the *sender's* lat/lon so that we can
                # calculate the distance between the sender's position
                # and the call sign that he has requested. We are only
                # interested in the user's lat/lon info and ignore the
                # remaining information such as callsign, altitude and lasttime
                (
                    success,
                    users_latitude,
                    users_longitude,
                    _,
                    _,
                    _,
                    _,
                ) = get_position_on_aprsfi(
                    aprsfi_callsign=users_callsign,
                    aprsdotfi_api_key=aprsdotfi_api_key,
                )
            elif what == "cwop":
                human_readable_message = f"CWOP for {message_callsign}"
                what = "cwop_by_latlon"
            elif what in ("metar", "taf"):
                icao = get_nearest_icao(latitude=latitude, longitude=longitude)
                if icao:
                    (
                        success,
                        latitude,
                        longitude,
                        metar_capable,
                        icao,
                    ) = validate_icao(icao_code=icao)
                    if success:
                        found_my_keyword = True
                        human_readable_message = f"{what.upper()} for '{icao}'"
                        # If we did find the airport but it is not METAR-capable,
                        # then supply a wx report instead
                        if not metar_capable:
                            what = "wx"
                            icao = None
                            human_readable_message = f"Wx for '{icao}'"
                    else:
                        icao = None
        else:
            human_readable_message = (
                f"{errmsg_cannot_find_coords_for_user} {message_callsign}"
            )
            kw_err = True

    parser_rd_csm = {
        "latitude": latitude,
        "longitude": longitude,
        "users_latitude": users_latitude,
        "users_longitude": users_longitude,
        "lasttime": lasttime,
        "comment": comment,
        "altitude": altitude,
        "what": what,
        "human_readable_message": human_readable_message,
        "aprs_message": aprs_message,
        "message_callsign": message_callsign,
        "city": city,
        "state": state,
        "county": county,
        "country": country,
        "country_code": country_code,
        "district": district,
        "address": address,
        "zipcode": zipcode,
        "street": street,
        "street_number": street_number,
        "icao": icao,
    }
    return found_my_keyword, kw_err, parser_rd_csm


def parse_what_keyword_whereami(
    aprs_message: str, users_callsign: str, aprsdotfi_api_key: str, language: str = "en"
):
    """

    Keyword parser for the 'whereami' command

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message
    users_callsign : 'str'
        Call sign of the user that has sent us the message
    aprsdotfi_api_key : 'str'
        aprs.fi access key
    language: 'str'
        ISO639-a2 language code

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    kw_err: 'bool'
        True if an error has occurred. If found_my_keyword is also true,
        then the error marker overrides the 'found' keyword
    parser_rd_whereami: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    found_my_keyword = kw_err = success = False
    human_readable_message = what = comment = None
    latitude = longitude = users_latitude = users_longitude = 0.0
    altitude = 0
    lasttime = datetime.min
    what = message_callsign = city = state = county = None
    zipcode = country = country_code = district = address = street = street_number = (
        None
    )

    regex_string = r"\b(whereami)\b"
    matches = re.search(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        what = "whereis"
        message_callsign = users_callsign
        found_my_keyword = True
        human_readable_message = f"Pos for {message_callsign}"
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()

        # Try to get the user's position on aprs.fi
        (
            success,
            latitude,
            longitude,
            altitude,
            lasttime,
            comment,
            message_callsign,
        ) = get_position_on_aprsfi(
            aprsfi_callsign=users_callsign,
            aprsdotfi_api_key=aprsdotfi_api_key,
        )
        if not success:
            kw_err = True
            human_readable_message = (
                f"{errmsg_cannot_find_coords_for_user} {message_callsign}"
            )
        else:
            # Finally, try to get the user's human readable address
            # we ignore any errors as all output fields will be properly initialized with default values
            success, response_data = get_reverse_geopy_data(
                latitude=latitude, longitude=longitude, language=language
            )
            # extract response fields; one/all can be 'None'
            city = response_data["city"]
            state = response_data["state"]
            country_code = response_data["country_code"]
            country = response_data["country"]
            district = response_data["district"]
            address = response_data["address"]
            zipcode = response_data["zipcode"]
            county = response_data["county"]
            street = response_data["street"]
            street_number = response_data["street_number"]

            # Finally, set the user's latitude / longitude
            # which -as we request our own position- is the
            # same latitude/longitude. Our target output
            # function will recognise these values and know that
            # the user's distance between these coordinates is
            # zero and then refrain from trying to calculate
            # any distance values
            users_latitude = latitude
            users_longitude = longitude

    parser_rd_whereami = {
        "latitude": latitude,
        "longitude": longitude,
        "users_latitude": users_latitude,
        "users_longitude": users_longitude,
        "lasttime": lasttime,
        "comment": comment,
        "altitude": altitude,
        "what": what,
        "human_readable_message": human_readable_message,
        "aprs_message": aprs_message,
        "message_callsign": message_callsign,
        "city": city,
        "state": state,
        "county": county,
        "country": country,
        "country_code": country_code,
        "district": district,
        "address": address,
        "zipcode": zipcode,
        "street": street,
        "street_number": street_number,
    }
    return found_my_keyword, kw_err, parser_rd_whereami


def build_human_readable_address_message(response_data: dict):
    """
    Build the 'human readable message' based on the reverse-lookup
    from OpenStreetMap

    Note: State information is ignored unless country_code=US. OSM does not
    provide 'state' information in an abbreviated format and we need
    to keep the message as brief as possible

    Parameters
    ==========
    response_data : 'dict'
        Dictionary as received via get_reverse_geopy_data()

    Returns
    =======
    human_readable_message: 'str'
        The human readable message string
    """

    human_readable_message = ""
    city = response_data["city"]
    state = response_data["state"]
    country_code = response_data["country_code"]
    country = response_data["country"]
    district = response_data["district"]
    address = response_data["address"]
    zipcode = response_data["zipcode"]
    county = response_data["county"]
    if city:
        human_readable_message = city
        if country_code:
            if country_code == "US":
                if state:
                    human_readable_message += f",{state}"
        if zipcode:
            human_readable_message += f",{zipcode}"
    if not city:
        if county:
            human_readable_message = county
    if country_code:
        human_readable_message += f";{country_code}"

    return human_readable_message


def get_units_based_on_users_callsign(users_callsign: str):
    """
    Based on the user's call sign (the user who has sent us the APRS
    message, we set the default unit of measure. Per Wikipedia, there
    are only three countries in the world that still use the imperial
    system: the U.S., Liberia and Myanmar. Users from these countries
    will get their results related to the imperial system whereas the
    rest of the world will use the metric system as default.

    Parameters
    ==========
    users_callsign : 'str'
        Call sign of the user that has sent us the APRS message

    Returns
    =======
    units: 'str'
        Can be either "metric" or "imperial". Default is "metric"
    """
    units = "metric"

    # Check if we need to switch to the imperial system
    # Have a look at the user's call sign who has sent me the message.
    # Ignore any SSID data.
    # If my user is located in the U.S., then assume that user wants data
    # not in metric but in imperial format. Note: this is an auto-prefix
    # which can be overridden by the user at a later point in time
    # Note: we do NOT examine any call sign within the APRS message text but
    # have a look at the (source) user's call sign
    matches = re.search(
        pattern=r"^[AKNW][a-zA-Z]{0,2}[0-9][A-Z]{1,3}",
        string=users_callsign,
        flags=re.IGNORECASE,
    )
    if matches:
        units = "imperial"
    # Now do the same thing for users in Liberia and Myanmar - per Wikipedia,
    # these two countries also use the imperial system
    matches = re.search(
        pattern=r"^(A8|D5|EL|5L|5M|6Z|XY|XZ)",
        string=users_callsign,
        flags=re.IGNORECASE,
    )
    if matches:
        units = "imperial"
    return units


def parse_keyword_units(aprs_message: str):
    """
    Keyword parser for the case where the user wants to override
    the default 'unit of measure' (metric or imperial) which is
    determined by having a look at the user's call sign - see
    get_units_based_on_users_callsign(). This function however
    takes a look at the user's MESSAGE and allows the user to
    override the based-on-callsign default setting

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    parser_rd_units: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    found_my_keyword = False
    units = "metric"

    # check if the user wants to change the numeric format
    # metric is always default, but we also allow imperial
    # format if the user explicitly asks for it
    # hint: these settings are not tied to the program's
    # duty roster so if we find this keyword we will NOT set the
    # duty roster marker and treat this as a 'what' command
    regex_string = r"\b(mtr|metric)\b"
    matches = re.search(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        units = "metric"
        found_my_keyword = True
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()

    regex_string = r"\b(imp|imperial)\b"
    matches = re.search(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        units = "imperial"
        found_my_keyword = True
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()

    parser_rd_units = {
        "aprs_message": aprs_message,
        "units": units,
    }
    return found_my_keyword, parser_rd_units


def parse_keyword_language(aprs_message: str):
    """
    Keyword parser for the case where the user wants to set a specific language

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    parser_rd_language: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    found_my_keyword = False
    language = "en"

    # check if the user wants to change the language
    # hint: setting is not tied to the program's duty roster
    regex_string = r"\b(lang|lng)\s*([a-zA-Z]{2})\b"
    matches = re.search(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        language = matches[2].lower().strip()
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()
        found_my_keyword = True

    parser_rd_language = {
        "aprs_message": aprs_message,
        "language": language,
    }
    return found_my_keyword, parser_rd_language


def parse_keyword_number_of_results(aprs_message: str):
    """
    Keyword parser for the case where the user wants more than one result

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    parser_rd_number_of_results: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    found_my_keyword = False
    number_of_results = 1

    regex_string = r"\btop(2|3|4|5)\b"
    matches = re.search(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        try:
            number_of_results = int(matches[1])
        except (ValueError, IndexError) as e:
            number_of_results = 1
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()
        found_my_keyword = True

    parser_rd_number_of_results = {
        "aprs_message": aprs_message,
        "number_of_results": number_of_results,
    }
    return found_my_keyword, parser_rd_number_of_results


def parse_keyword_unicode(aprs_message: str):
    """
    Keyword parser for the utf8 command (user demands that we send the
    outgoing message in UTF-8 and don't downgrade the content to ASCII)

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    parser_rd_unicode: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    found_my_keyword = False
    force_outgoing_unicode_messages = False

    regex_string = r"\bunicode\b"
    matches = re.search(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        try:
            force_outgoing_unicode_messages = True
        except:
            force_outgoing_unicode_messages = False
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()
        found_my_keyword = True

    parser_rd_unicode = {
        "aprs_message": aprs_message,
        "force_outgoing_unicode_messages": force_outgoing_unicode_messages,
    }
    return found_my_keyword, parser_rd_unicode


def parse_what_keyword_fortuneteller(aprs_message: str):
    """
    Keyword parser for our fortuneteller language + UTF-8 testing

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    kw_err: 'bool'
        True if an error has occurred (not used in this function)
    parser_rd_fortuneteller: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    found_my_keyword = kw_err = False
    what = None

    regex_string = r"\b(fortuneteller|magic8ball|magic8|m8b)\b"
    matches = re.search(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        what = "fortuneteller"
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()
        found_my_keyword = True

    parser_rd_fortuneteller = {
        "aprs_message": aprs_message,
        "what": what,
    }
    return found_my_keyword, kw_err, parser_rd_fortuneteller


def parse_what_keyword_email_position_report(
    aprs_message: str, users_callsign: str, aprsdotfi_api_key: str, language: str = "en"
):
    """
    Keyword parser email position report
    This is literally a carbon copy of the WHEREAMI keyword.
    The slight difference is that this keyword here will
    generate an email with the user's position data whereas
    WHEREAM will generate APRS messages

    Parameters
    ==========
    aprs_message : 'str'
        the original aprs message
    users_callsign : 'str'
        Call sign of the user that has sent us the message
    aprsdotfi_api_key : 'str'
        aprs.fi access key
    language: 'str'
        ISO639-a2 language code

    Returns
    =======
    found_my_keyword: 'bool'
        True if the keyword and associated parameters have been found
    kw_err: 'bool'
        True if an error has occurred. If found_my_keyword is also true,
        then the error marker overrides the 'found' keyword
    parser_rd_email_posrpt: 'dict'
        response data dictionary, containing the keyword-relevant data
    """

    found_my_keyword = kw_err = success = False
    human_readable_message = what = mail_recipient = comment = None
    latitude = longitude = users_latitude = users_longitude = 0.0
    altitude = 0
    lasttime = datetime.min
    what = message_callsign = city = state = county = None
    zipcode = country = country_code = district = address = street = street_number = (
        None
    )

    # check for a keyword - email pattern
    regex_string = (
        r"\b(posmsg|posrpt)\s*([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$)\b"
    )
    matches = re.search(pattern=regex_string, string=aprs_message, flags=re.IGNORECASE)
    if matches:
        mail_recipient = matches[2].strip()
        aprs_message = re.sub(
            pattern=regex_string, repl="", string=aprs_message, flags=re.IGNORECASE
        ).strip()
        found_my_keyword = True
        what = "email_position_report"
        message_callsign = users_callsign
        human_readable_message = f"Email position for {message_callsign}"

        # Try to get the user's position on aprs.fi
        (
            success,
            latitude,
            longitude,
            altitude,
            lasttime,
            comment,
            message_callsign,
        ) = get_position_on_aprsfi(
            aprsfi_callsign=users_callsign,
            aprsdotfi_api_key=aprsdotfi_api_key,
        )
        if not success:
            kw_err = True
            human_readable_message = (
                f"{errmsg_cannot_find_coords_for_user} {message_callsign}"
            )
        else:
            # Finally, try to get the user's human readable address
            # we ignore any errors as all output fields will be properly initialized with default values
            success, response_data = get_reverse_geopy_data(
                latitude=latitude,
                longitude=longitude,
                language=language,
                disable_state_abbreviation=True,
            )
            # extract response fields; one/all can be 'None'
            city = response_data["city"]
            state = response_data["state"]
            country = response_data["country"]
            country_code = response_data["country_code"]
            district = response_data["district"]
            address = response_data["address"]
            zipcode = response_data["zipcode"]
            county = response_data["county"]
            street = response_data["street"]
            street_number = response_data["street_number"]

            # Finally, set the user's latitude / longitude
            # which -as we request our own position- is the
            # same latitude/longitude. Our target output
            # function will recognise these values and know that
            # the user's distance between these coordinates is
            # zero and then refrain from trying to calculate
            # any distance values
            users_latitude = latitude
            users_longitude = longitude

    parser_rd_email_posrpt = {
        "what": what,
        "human_readable_message": human_readable_message,
        "aprs_message": aprs_message,
        "mail_recipient": mail_recipient,
        "latitude": latitude,
        "longitude": longitude,
        "users_latitude": users_latitude,
        "users_longitude": users_longitude,
        "lasttime": lasttime,
        "comment": comment,
        "altitude": altitude,
        "message_callsign": message_callsign,
        "city": city,
        "state": state,
        "county": county,
        "country": country,
        "country_code": country_code,
        "district": district,
        "address": address,
        "zipcode": zipcode,
        "street": street,
        "street_number": street_number,
    }
    return found_my_keyword, kw_err, parser_rd_email_posrpt


if __name__ == "__main__":
    (
        success,
        aprsdotfi_api_key,
        aprsis_callsign,
        aprsis_passcode,
        dapnet_callsign,
        dapnet_passcode,
        smtpimap_email_address,
        smtpimap_email_password,
        apprise_config_file,
    ) = read_program_config()
    logger.info(pformat(parse_input_message("taf eddf", "df1jsl-1", aprsdotfi_api_key)))
