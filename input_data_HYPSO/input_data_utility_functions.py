import csv
import json
from dataclasses import dataclass, asdict
import skyfield.api as skf
from requests.exceptions import SSLError, RequestException
import os
import requests
import datetime

from scheduling_model import GS, PH, GT




def csvToDict(filepath) -> dict:
    """
    Reads a CSV file and returns a dictionary where each row's first column is the key and the second column is the value.
    If several rows after the first, the values are added as a list
    Ignores rows starting with #.
    Output:
    - dict: dictionary with key-value pairs ( the first and second element of each row) from the CSV file
    """
    dict= {}
    with open(filepath, mode='r', newline='') as csvfile:
        reader = csv.reader(csvfile)
        for row in reader:
            if not row or row[0].strip().startswith('#'):
                continue
            if len(row) == 2:
                key = row[0].strip()
                value = row[1].strip()
                dict[key] = value
            if len(row) > 2:
                key = row[0].strip()
                values = [value.strip() for value in row[1:]]
                dict[key] = values
    return dict

###----------------------------------------------------------------------###
### GET INPUT PARAMETERS FROM CSV FILE ###

@dataclass
class InputParameters:
    
    # Scheduling specific parameters
    captureDuration: int
    planningStart: datetime
    planningEnd: datetime
    hypsoNr: int

    def __init__(self, captureDuration: int, planningStart: datetime, planningEnd: datetime, hypsoNr: int):
        self.captureDuration = captureDuration
        self.planningStart = planningStart
        self.planningEnd = planningEnd
        self.hypsoNr = hypsoNr

    @classmethod
    def fromCsv(cls, filepath: str):
        """Create InputParameters from CSV file"""
        params_dict = csvToDict(filepath)
        return cls(
            captureDuration=int(params_dict['captureDuration']),
            planningStart=datetime.datetime.strptime(params_dict['startTimePlanning'], "%Y-%m-%dT%H:%M:%SZ"),
            planningEnd=datetime.datetime.strptime(params_dict['endTimePlanning'], "%Y-%m-%dT%H:%M:%SZ"),
            hypsoNr=int(params_dict['hypsoNr'])
        )
    
    @classmethod
    def fromJson(cls, filePath: str):
        """Create InputParameters from JSON file"""
        with open(filePath, "r") as f:
            data = json.load(f)
        return cls(**data)

    def toJson(self) -> str:
        """Convert InputParameters instance to JSON string"""
        return json.dumps(asdict(self), indent=4)
    

###----------------------------------------------------------------------###
### GET TARGET DATA FROM JSON FILE ###


@dataclass
class TargetData:
    name: str
    lat: float
    lon: float
    elev: float
    cc: float
    exp: float
    mode: str
    night: int
    t0: str
    t1: str


def getTargetDataFromJsonFile(targetsJsonFile: str):
    """ 
    Get the target data from a JSON file
    Input: path to the JSON file
    Output: List of TargetData objects 
    """
    with open(targetsJsonFile, 'r') as f:
        targets_json = json.load(f)
        
    targets: list[TargetData] = []
    for json_obj in targets_json:
        valid_keys = {field.name for field in TargetData.__dataclass_fields__.values()}
        filtered = {k: v for k, v in json_obj.items() if k in valid_keys}
        targets.append(TargetData(**filtered))
    
    return targets

def getTargetIdPriorityDictFromJson(targetsJsonFile: str) -> dict:
    """ 
    Get a dictionary mapping target IDs to their priorities from a JSON file
    Input: path to the JSON file
    Output: Dictionary with target ID as key and priority as value
    """
    if not os.path.exists(targetsJsonFile):
        raise FileNotFoundError(f"File not found: {targetsJsonFile}")
    
    with open(targetsJsonFile, 'r') as f:
        targets = json.load(f)
    
    priorityIdDict = {}
    for index, target in enumerate(targets):
        targetId = target['name'].strip()  # Remove any whitespace
        targetPriority = len(targets) - index
        
        priorityIdDict[targetId] = targetPriority
    return priorityIdDict

def updateTargetJsonFile():
    # make a function that reads from most updated traget file if it has been some time since last time checked, 
    # update the json file if so 
    return

def getGroundTargetList() -> list[GT]:
    """Get the ground target list from the JSON file and convert it to a list of GT namedtuples"""
    targetsJsonFile = os.path.join(os.path.dirname(__file__), "targets.json")
    targetDataList = getTargetDataFromJsonFile(targetsJsonFile)
    targetIdPriorityDict = getTargetIdPriorityDictFromJson(targetsJsonFile)
    gtList = []
    gt_ids = set()
    for targetData in targetDataList:
        if targetData.name in gt_ids:
            print(f"Warning: Duplicate target name '{targetData.name}' found in targets.json. Skipping this entry.")
            continue
        gt_ids.add(targetData.name)
        gtList.append(GT(
            id=targetData.name,
            lat=targetData.lat,
            long=targetData.lon,
            priority=targetIdPriorityDict.get(targetData.name, 0), 
            cloudCoverage=targetData.cc,
            exposureTime=targetData.exp,
            captureMode=targetData.mode
        ))
    return gtList



###----------------------------------------------------------------------###
### GET THE GROUND STATIONS DATA FOR THE HYPSO SATELLITES ###

def getGroundStationList() -> list:
    """Read ground station rows from a CSV file and return a list of GS objects."""

    groundStationsCsvFile: str = os.path.join(os.path.dirname(__file__), "ground_stations.csv")

    groundStations: list[GS] = []
    with open(groundStationsCsvFile, mode='r', newline='') as csvfile:
        reader = csv.reader(csvfile, delimiter=';')

        for row in reader:
            if not row:
                continue

            firstField = row[0].strip()
            if not firstField or firstField.startswith('#') or firstField.lower() in {'name', 'id'}:
                continue

            cleanedRow = [field.strip() for field in row if field.strip() != '']

            groundStations.append(GS(
                id=cleanedRow[0],
                lat=float(cleanedRow[1]),
                long=float(cleanedRow[2]),
                minElevation=float(cleanedRow[3])
            ))

    return groundStations

###----------------------------------------------------------------------###
### GET THE TLE DATA FOR THE HYPSO SATELLITES ###

def updateTLE (HYPSOnr: int, TLEupdateThresholdHours: int = 34):

    url = f'https://celestrak.com/NORAD/elements/gp.php?NAME=HYPSO-{HYPSOnr}&FORMAT=TLE'
    filename = os.path.join(os.path.dirname(__file__), f"HYPSO-{HYPSOnr}_TLE.txt")

    skf_hypso = skf.load.tle_file(url, filename=filename, reload=False)[0]
    ts = skf.load.timescale()
    TLE_age = ts.now().utc_datetime() - skf_hypso.epoch.utc_datetime()

    TLE_age_hours = TLE_age.days*24 + TLE_age.seconds/3600.0

    if TLE_age_hours > TLEupdateThresholdHours:
        print(f'current TLE is {TLE_age_hours:7.4f} hours old')
        try:
            tle = requests.get(url, timeout=15)
            tle.raise_for_status()
        except SSLError as e:
            print("SSL error when fetching TLE:", e)
            print("Check system date/time, proxy/captive-portal, or CA bundle.")
            # Insecure fallback only if you explicitly opt in (risky)
            try:
                print("Attempting insecure fallback (verify=False)...")
                tle = requests.get(url, timeout=15, verify=False)
                tle.raise_for_status()
            except RequestException as e2:
                print("Fallback failed:", e2)
                print("TLE Update not successful")
                return
        except RequestException as e:
            print("Network error when fetching TLE:", e)
            print("TLE Update not successful")
            return

        # write file if we have successful response
        with open(filename, 'w') as file:
            file.write(tle.text)
        print('TLE update successful\n')
    else:
        # print(f'Skipping TLE update, current TLE is only {TLE_age_hours:7.4f} hours old')
        return

def getTLEfilePath (HYPSOnr: int) -> str:

    # Update TLE if older than 34 hours
    updateTLE(HYPSOnr, TLEupdateThresholdHours=34)

    return os.path.join(os.path.dirname(__file__), f"HYPSO-{HYPSOnr}_TLE.txt")

    