import csv
from bisect import bisect_left
from datetime import datetime, timezone, timedelta
import os

from dotenv import load_dotenv

import requests
from requests.auth import HTTPBasicAuth

from find_satellite_passes.extract_cloud_data import getCloudData
from find_satellite_passes.orbital_calculations import findSatelliteTargetPasses, findDaylightPeriods
from input_data_HYPSO.input_data_utility_functions import getGroundStartionList, getGroundTargetList
from scheduling_model import GT, TW, TTW, GSTW, GS



def getTTWList(captureTimeSeconds: int, startTimeOH: datetime, endTimeOH: datetime, hypsoNr: int, 
               daylightFilter: bool = True, cloudFilter: bool = True) -> list:
    """ Get the TTW for each time the satellite passes the every requested target
    Output:
    - allTargetPasses: list of TTWs for each target in the target request file
    """

    groundTargets = getGroundTargetList()
    allTargetPasses = []
    #Loop through the targets and calculate earliest start time and latest start time for capturing
    for target in groundTargets:

        # Find the time windows when satellite is passing the targets. Each element in pass is a tuple : [utc_time, type('rise', 'culiminate', 'set')]
        passes = findSatelliteTargetPasses(float(target.lat), float(target.lon), float(target.elev), startTimeOH, endTimeOH, hypsoNr)

        # Skip iteration if no passes are found
        if not passes:
            continue

        #For each target pass, find start time and end time of the time window
        startTimesUtc = []
        endTimesUtc = []
        for i in range(len(passes) - 2):
            if passes[i][1] == 'rise' and passes[i + 1][1] == 'culminate' and passes[i + 2][1] == 'set':
                # The pass i -> i+2 corresponds to a time window

                time_diff = (passes[i + 2][0] - passes[i][0]).total_seconds()
                if time_diff < captureTimeSeconds or time_diff > 2000: # 2000 s is a hardcoded upper limit just to verify the skyfield calculations are not mistaking long passes for multiple passes.
                    # Time window too short or too long
                    continue

                # Add tw to start and end times
                startTimesUtc.append(passes[i][0])
                endTimesUtc.append(passes[i + 2][0])


        # If all start times are removed by time constraints, go to next target
        if len(startTimesUtc) == 0:
            continue

        # Check that number of start times is equal number of end times
        if len(startTimesUtc) != len(endTimesUtc):
            print(f"len passes: {len(passes)}, firstpass: {passes[0][1]}, lastpass: {passes[-1][1]}, len startTimes: {len(startTimesUtc)}, len endTimes: {len(endTimesUtc)}")
            for p in passes:
                print(p)
            raise ValueError("The length of start times and end times are not equal")

        # Create a target Pass object 
        targetPass = {}
        targetPass['groundTarget'] = target
        targetPass['startTimes'] = startTimesUtc
        targetPass['endTimes'] = endTimesUtc

        allTargetPasses.append(targetPass)

    return allTargetPasses

def removeNonIlluminatedPasses(allTargetPasses: list, startTimeOH: datetime, endTimeOH: datetime)-> list:
    """ Remove time windows where the target is not illuminated by the sun
    Output:
    - targetPassesWithIllumination: list of TTWs that have sufficient illumination
    """
    targetPassesWithIllumination = []    
    
    for targetPass in allTargetPasses:
        gt = targetPass['groundTarget']
        latitude = float(gt.lat)
        longitude = float(gt.long)
        startTimes = targetPass['startTimes'].copy()

        illuminatedPeriods = findIllumminationPeriods(float(latitude), float(longitude), startTimeOH, endTimeOH)

        for st in startTimes:
            # Loop through all TWs of the target
            illuminated = False
            
            # Check that the TW is within an illuminated period
            for sunRise, sunSet in illuminatedPeriods:
                if sunRise <= st <= sunSet:
                    # This TW is illuminated, go next TW
                    illuminated = True
                    break

            if not illuminated:
                # TW is not illuminated, remove it
                index = targetPass['startTimes'].index(st)
                targetPass['startTimes'].pop(index)
                targetPass['endTimes'].pop(index)

        if len(targetPass['startTimes']) > 0:        
            targetPassesWithIllumination.append(targetPass)

    return targetPassesWithIllumination
  
def createGSTWList(startTimeOH: datetime, endTimeOH: datetime, minWindowLength: float,
                                hypsoNr: int, groundStationsFilePath: str = None, commInterface: str = "xband") -> list[GSTW]:
    """
    Get the time windows when the satellite passes over one of the ground stations.

    Args:
        startTimeOH (datetime): Start time of the observation horizon.
        endTimeOH (datetime): End time of the observation horizon.
        minWindowLength (float): Minimum length of a time window in seconds.
        groundStationsFilePath (str): Path to the ground stations file.
        hypsoNr (int): HYPSO satellite number.
        commInterface (str): Type of communication that is used with ground station.

    Returns:
        list[GSTW]: List of ground stations and their time windows.
    """

    # Bookings at KSAT Svalbard ground station are only made 3 days in advance and past bookings are not available via the API
    now = datetime.now(timezone.utc)
    print("Not looking at KSAT Svalbard booked passes, as this feature is currently disabled.")
    ## Comment out if statement below to disable booked passes from KSAT Svalbard ground station
    # if (endTimeOH - now).total_seconds() < 3 * 24 * 3600 and  \
    #     (startTimeOH - now).total_seconds() > 0:

    #     return getBookedGSTWList(startTimeOH, endTimeOH, hypsoNr, commInterface)

    return createGSTWListFromFile(startTimeOH, endTimeOH, minWindowLength, hypsoNr, groundStationsFilePath)


def getBookedGSTWList(startTimeOH: datetime, endTimeOH: datetime, hypsoNr: int, commInterface: str = "xband") -> list[GSTW]:
    """
    Get the timeslots for the passes over KSAT Svalbard ground station that are booked for the HYPSO satellites.
    Timeslots are only booked when it is possible for the satellite to have a stable connection.
    """

    gs = GS("ksatsvalbard", "78.2208", "15.4260", "5")  # KSAT Svalbard ground station

    # Retrieve booked timeslots
    url = f"https://ops.monitoring.hypso.space/api/v1/contacts/sat/HYPSO{hypsoNr}"
    load_dotenv()
    password = os.getenv("KSAT_PASSES_DASHBOARD_AUTH_TOKEN")
    response = requests.get(url, auth=HTTPBasicAuth("ntnu", password))
    response.raise_for_status()
    gstwJson = response.json()

    # Extract timestamp data
    timestamps = []
    for timestamp_struct in gstwJson:
        start = timestamp_struct['Start']
        end = timestamp_struct['End']
        interface = timestamp_struct['Interface']
        start = datetime.strptime(start, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        end = datetime.strptime(end, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)

        if interface == commInterface and start >= startTimeOH and end <= endTimeOH:
            timestamps.append([start, end])
    # Create GSTW object
    twList: list[TW] = []
    for ts in timestamps:
        startTime = (ts[0] - startTimeOH).total_seconds()
        endTime = (ts[1] - startTimeOH).total_seconds()
        twList.append(TW(startTime, endTime))
    gstwList = [GSTW(gs, twList)]
    return gstwList

def createGSTWListFromFile(startTimeOH: datetime, endTimeOH: datetime, minWindowLength: float,
                                hypsoNr: int, groundStationsFilePath: str = None) -> list[GSTW]:
    """
    Get the passes over the ground stations defined in the ground station file.
    Determine the time windows of the passes using orbital information.
    """

    # If no file path is provided, use default path
    if groundStationsFilePath is None:
        groundStationsFilePath = os.path.join(os.path.dirname(__file__), "../data_input/HYPSO_data/ground_stations.csv")

    # Read data from the provided csv
    try:
        with open(groundStationsFilePath, mode='r') as file:
            reader = csv.reader(file)
            # Read the data from the csv
            groundStations = [row for row in reader]
            # Remove the first row (header)
            groundStations.pop(0)

    except FileNotFoundError:
        print(f"File not found: {groundStationsFilePath}")
    except Exception as e:
        print(f"An error occurred: {e}")

    gstwList: list[GSTW] = []

    # Loop through the stations and calculate the earliest start time and latest start time for downlinking
    for index, groundStation in enumerate(groundStations):

        # Create Ground Station object
        groundStation = groundStation[0].split(';')
        gs = GS(
            id=groundStation[0],
            lat=groundStation[1],
            long=groundStation[2],
            minElevation=groundStation[3]
        )

        # Find the time windows when satellite is passing the ground station. Each element in pass is a tuple : [utc_time, type('rise', 'culiminate', 'set')]
        passes = findSatelliteTargetPasses(float(gs.lat), float(gs.long), float(gs.minElevation), startTimeOH, endTimeOH, hypsoNr)

        # Skip iteration if no passes are found
        if not passes:
            continue

        # For each target pass, find start time and end time of the time window
        twList: list[TW] = []
        for i in range(len(passes) - 2):
            if passes[i][1] == 'rise' and passes[i + 1][1] == 'culminate' and passes[i + 2][1] == 'set':
                # The pass i -> i+2 corresponds to a time window
                startTime = (passes[i][0] - startTimeOH).total_seconds()
                endTime = (passes[i + 2][0] - startTimeOH).total_seconds()
                if endTime - startTime >= minWindowLength:
                    twList.append(TW(startTime, endTime))

        gstwList.append(GSTW(gs, twList))

    if len(gstwList) == 0:
        raise ValueError("No ground station passes found")

    return gstwList

def removeCloudObscuredPasses(allTargetPasses: list, startTimeOH: datetime, endTimeOH: datetime)-> list:
    """ Remove time windows where the target is obscured by clouds, based on weather forecast data
    Output:
    - targetPassesWithoutClouds: list of TTWs that are not obscured by clouds
    """

    targetPassesWithoutClouds = []

    for targetPass in allTargetPasses:

        gt = targetPass['groundTarget']

        latitude = float(gt.lat)
        longitude = float(gt.long)
        startTimes = targetPass['startTimes'].copy()
        maxCloudCoverage = gt.cloudCoverage

        # Get the cloud data for the target in the given OH
        cloudData = getCloudData(latitude, longitude, startTimeOH, endTimeOH)
        assert cloudData is not None
        if len(cloudData) == 0:
            targetPassesWithoutClouds.append(targetPass)
            continue

        predictionTimes = sorted(cloudData.keys())
        # Loop over timestamps when target is passed and find the closest cloud data prediction
        for startTime in startTimes:
            # Find the closest prediction time to the start time of the target pass
            pos = bisect_left(predictionTimes, startTime)
            if pos == 0:
                closestTime = predictionTimes[0]
            elif pos == len(predictionTimes):
                closestTime = predictionTimes[-1]
            else:
                before = predictionTimes[pos - 1]
                after = predictionTimes[pos]
                if abs((after - startTime).total_seconds()) < abs((startTime - before).total_seconds()):
                    closestTime = after
                else:
                    closestTime = before

            if cloudData[closestTime] > float(maxCloudCoverage):
                index = targetPass['startTimes'].index(startTime)
                targetPass['startTimes'].pop(index)
                targetPass['endTimes'].pop(index)

        # If target has observation windows left, add it to the list of targets without clouds
        if len(targetPass['startTimes']) > 0:
            targetPassesWithoutClouds.append(targetPass)

    return targetPassesWithoutClouds

def createOH(startTimeOH: datetime, ohDurationInDays: int) -> OH:
    """ Create optimization horizon object form input parameters, and print the time interval.
    Output:
     - oh: OH object
    """

    endTimeOH = startTimeOH + timedelta(days=ohDurationInDays)
    # print("Start time OH:", startTimeOH.strftime('%Y-%m-%dT%H:%M:%SZ'), "End time OH:", endTimeOH.strftime('%Y-%m-%dT%H:%M:%SZ'))

    oh = OH(
        utcStart=startTimeOH,
        utcEnd=endTimeOH
    )

    return oh


def createTTWList(captureDuration: int, oh: OH, hypsoNr: int, targetsFilePath: str, ttwFilePathRead: str = None, ttwFilePathWrite: str = None) -> list:
    """ Calculate the satellite passes and store in data objects defined in scheduling_model.py

    Args:
        captureDuration (int): Duration of each image capture in seconds.
        oh (OH): Observation Horizon object containing start and end times.
        hypsoNr (int): HYPSO satellite number.
        ttwFilePathRead (str, optional): File path to read pre-calculated TTW data. If provided, TTW data will be read from this file instead of being calculated. Defaults to None.
        ttwFilePathWrite (str, optional): File path to write calculated TTW data. If provided, calculated TTW data will be saved to this file. Defaults to None.

    Returns:
        tuple: A tuple containing two elements:
            - list of TTW objects representing target time windows.
            - list of GSTW objects representing ground station time windows.
    """

    # If a file path is provided for the TTW data, read the data from the file instead of calculating it
    if ttwFilePathRead is not None:
        ttwList = getTTWListFromFile(ttwFilePathRead)
        if ttwList is not None:
            return ttwList
        else:
            print("Error reading TTW data from file, calculating TTW data instead")

    # Update TLE
    # updateTLE(hypsoNr)

    # Get the target passes
    allTargetPasses = getAllTargetPasses(captureDuration, oh.utcStart, oh.utcEnd, targetsFilePath, hypsoNr)
    # print(f"Without filtering, targets: {len(allTargetPasses)}, captures: {howManyPasses(allTargetPasses)}")
    
    # Filter out night passes that are not illuminated by the sun
    illuminatedPasses = removeNonIlluminatedPasses(allTargetPasses, oh.utcStart, oh.utcEnd)
    # print(f"After filtering out non-illuminated passes, targets: {len(illuminatedPasses)}, captures: {howManyPasses(illuminatedPasses)}")
    
    # Filter out targets that are obscured by clouds
    cloudlessTargetpasses = illuminatedPasses
    print(f"Skipping cloud coverage filtering step.")
    #cloudlessTargetpasses = removeCloudObscuredPasses(illuminatedPasses, oh.utcStart, oh.utcEnd)
    # print(f"After filtering out cloud-obscured passes, targets: {len(cloudlessTargetpasses)}, captures: {howManyPasses(cloudlessTargetpasses)}")

    # Create objects from the ground targets data
    ttwList = []
    for targetPass in cloudlessTargetpasses:
        twList = []

        # Create List of Time Window objects
        if len(targetPass['startTimes']) != len(targetPass['endTimes']):
            print("ERROR: The length of start times and end times are not equal")

        for i in range(len(targetPass['startTimes'])):
            tw = TW(
                start = (targetPass['startTimes'][i] - oh.utcStart).total_seconds(),
                end = (targetPass['endTimes'][i] - oh.utcStart).total_seconds() 
            )
            twList.append(tw)
        # Create Target Time Window object
        ttw = TTW(
            GT = targetPass['groundTarget'],
            TWs = twList
        )
        ttwList.append(ttw)

    if ttwFilePathWrite is not None:
        saveTTWListInJsonFile(ttwFilePathWrite, ttwList)

    return ttwList

def howManyPasses(targetPassList: list) -> tuple[int, int]:
    """ Return the total number of target passes in the OH """
    count = 0
    for targetDict in targetPassList:
        count += len(targetDict['startTimes'])
    return count, len(targetPassList)

