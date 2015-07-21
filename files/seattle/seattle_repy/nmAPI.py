""" 
Author: Justin Cappos

Module: Node Manager API.   This processes an already checked request.
    In almost all cases this means that no security checks are done here as
    a result.

Start date: September 1st, 2008

The design goals of this version are to be secure, simple, and reliable (in 
that order).   

The individual functions in here are called from nmrequesthandler.   The 
functions are listed in the API_dict.
"""

from repyportability import *
_context = locals()
add_dy_support(_context)

# For sys.executable, see #1081
import sys

# used to persist data...
import persist

# used to do resource file parsing and math...
import resourcemanipulation

# Is this the same as nmrequesthandler.BadRequest?  (the expected type of 
# exception)
class BadRequest(Exception):
  pass

# used to check file names in addfiletovessel, retrievefilefromvessel, and
# deletefileinvessel
from emulfile import check_repy_filename

# needed for path.exists and remove
import os 

# needed for copy
import shutil

import threading

import time

# used in startvessel and stopvessel
import statusstorage

# used in startvessel and resetvessel
import nmstatusmonitor

# used to check file size restrictions...
import nonportable

# Used for logging information.
import servicelogger

# This dictionary keeps track of all the programming
# platform that Seattle supports and where they are
# located.
prog_platform_dir = {'repyv1' : 'repyV1',
                     'repyv2' : 'repyV2'
                     }


# need this to check uploaded keys for validity
def rsa_is_valid_publickey(key):
  """
  <Purpose>
    This tries to determine if a key is valid.   If it returns False, the
    key is definitely invalid.   If True, the key is almost certainly valid
  
  <Arguments>
    key:
        A dictionary of the form {'n': 1.., 'e': 6..} with the 
        keys 'n' and 'e'.  
                  
  <Exceptions>
    None

  <Side Effects>
    None
    
  <Return>
    If the key is valid, True will be returned. Otherwise False will
    be returned.
    
  """
  # must be a dict
  if type(key) is not dict:
    return False

  # missing the right keys
  if 'e' not in key or 'n' not in key:
    return False

  # has extra data in the key
  if len(key) != 2:
    return False

  for item in ['e', 'n']:
    # must have integer or long types for the key components...
    if type(key[item]) is not int and type(key[item]) is not long:
      return False

  if key['e'] < key['n']:
    # Seems valid...
    return True
  else:
    return False
  
  

def rsa_publickey_to_string(publickey):
  """
  <Purpose>
    To convert a publickey to a string. It will read the
    publickey which should a dictionary, and return it in
    the appropriate string format.
  
  <Arguments>
    publickey:
              Must be a valid publickey dictionary of 
              the form {'n': 1.., 'e': 6..} with the keys
              'n' and 'e'.
    
  <Exceptions>
    ValueError if the publickey is invalid.

  <Side Effects>
    None
    
  <Return>
    A string containing the publickey. 
    Example: if the publickey was {'n':21, 'e':3} then returned
    string would be "3 21"
  
  """
  if not rsa_is_valid_publickey(publickey):
    raise ValueError, "Invalid public key"

  return str(publickey['e'])+" "+str(publickey['n'])


def rsa_string_to_publickey(mystr):
  """
  <Purpose>
    To read a private key string and return a dictionary in 
    the appropriate format: {'n': 1.., 'e': 6..} 
    with the keys 'n' and 'e'.
  
  <Arguments>
    mystr:
          A string containing the publickey, should be in the format
          created by the function rsa_publickey_to_string.
          Example if e=3 and n=21, mystr = "3 21"
          
  <Exceptions>
    ValueError if the string containing the privateky is 
    in a invalid format.

  <Side Effects>
    None
    
  <Return>
    Returns a publickey dictionary of the form 
    {'n': 1.., 'e': 6..} with the keys 'n' and 'e'.
  
  """
  if len(mystr.split()) != 2:
    raise ValueError, "Invalid public key string"
  
  return {'e':long(mystr.split()[0]), 'n':long(mystr.split()[1])}


# MIX: fix with repy <-> python integration changes
import random
randomfloat = random.random()

# Armon: Import Windows API so that processes can be launched
try:
  import windows_api as windowsAPI
  import repy_constants
except: # This will fail on non-windows platforms
  windowsAPI = None

# Armon: Import nonportable to use getruntime
import nonportable

# import for Popen
import portable_popen

offcutfilename = "resources.offcut"

# The node information (reported to interested clients)
nodename = ""
nodepubkey = ""
nodeversion = ''

# the maximum size the logging buffer can be
# NOTE: make sure this is equal to the maxbuffersize in logging.py
logmaxbuffersize = 16*1024

# the maximum length of the ownerstring
maxownerstringlength = 256


# The vesseldict is the heart and soul of the node manager.   It keeps all of 
# the important state for the node.   The functions that change this must 
# persist it afterwards.
# The format of an entry is:
#   'vesselname':{'userkeys':[key1, key2, ...], 'ownerkey':key, 
#   'oldmetadata':info, 'stopfilename':stopfile, 'logfilename':logfile, 
#   'advertise':True, 'ownerinformation':'...', 'status':'Fresh',
#   'resourcefilename':resourcefilename, 'statusfilename':statusfilename}
#
# The 'status' field is updated by the status monitor.   This thread only 
# reads it.
# 
# The 'advertise' field is read by the advertise thread.   This thread updates
# the value.
#
# The stopfilename is always the vesselname+'.stop', the logfilename is always 
# the vesselname+'.log', and the resourcefilename is 'resources.'+vesselname
# However, these are listed in the dictionary rather than derived when needed
# for clarity / ease of future changes.
#
# No item that is modified by an API call requires persistant updates to
# anything except the vesseldict.   The vesseldict must always be the
# last thing to be updated.   Since all actions that are performed on existing
# files are either atomic or read-only, there is no danger of corruption of
# the disk state.
vesseldict = {}


def initialize(name, pubkey, version):
  # Need to set the node name, etc. here...
  global nodename
  global nodepubkey
  global nodeversion
  global vesseldict
  

  nodename = name
  nodepubkey = pubkey

  nodeversion = version 

  # load the vessel from disk
  vesseldict = persist.restore_object('vesseldict')

  return vesseldict


def getvessels():
  # Returns vessel information
  # start with the node name, etc.
  vesselstring = "Version: "+nodeversion+"\n"
  vesselstring = vesselstring+"Nodename: "+nodename+"\n"
  vesselstring = vesselstring+"Nodekey: "+rsa_publickey_to_string(nodepubkey)+"\n"
  
  # for each vessel add name, status ownerkey, ownerinfo, userkey(s)
  for vesselname in vesseldict:
    vesselstring = vesselstring+"Name: "+vesselname+"\n"
    vesselstring = vesselstring+"Status: "+vesseldict[vesselname]['status']+"\n"
    vesselstring = vesselstring+"Advertise: "+str(vesseldict[vesselname]['advertise'])+"\n"
    vesselstring = vesselstring+"OwnerKey: "+rsa_publickey_to_string(vesseldict[vesselname]['ownerkey'])+"\n"
    vesselstring = vesselstring+"OwnerInfo: "+vesseldict[vesselname]['ownerinformation']+"\n"
    for userkey in vesseldict[vesselname]['userkeys']:
      vesselstring = vesselstring+"UserKey: "+rsa_publickey_to_string(userkey)+"\n"

  vesselstring = vesselstring + "\nSuccess"
  return vesselstring


def getvesselresources(vesselname):

  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"

  # return the resources file...
  resourcefo = open(vesseldict[vesselname]['resourcefilename'])
  resourcedata = resourcefo.read()
  resourcefo.close()
  resourcedata = resourcedata + "\nSuccess"
  return resourcedata

def getoffcutresources():
  # return the resources file...
  resourcefo = open(offcutfilename)
  resourcedata = resourcefo.read()
  resourcefo.close()
  resourcedata = resourcedata + "\nSuccess"
  return resourcedata


allowedchars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890.-_ "

def startvessel(vesselname, argstring):
  """
  This is the old startvessel call that will become obsolete eventually.
  startvessel now calls startvessel_ex using repyV1 as the programming
  language.
  """
  return startvessel_ex(vesselname, 'repyV1', argstring)




def startvessel_ex(vesselname, prog_platform, argstring):
  
  # Convert the programming platform to lowercase to make
  # it case insensitive.
  prog_platform = prog_platform.lower()

  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"

  if vesseldict[vesselname]['status'] == 'Started':
    raise BadRequest("Vessel has already been started")

  if prog_platform not in prog_platform_dir.keys():
    raise BadRequest("Programming language platform is not supported.")

  # remove any prior stop file so that we can start
  if os.path.exists(vesseldict[vesselname]['stopfilename']):
    os.remove(vesseldict[vesselname]['stopfilename'])

  for char in argstring:
    if char not in allowedchars:
      raise BadRequest("Character '"+char+"' not allowed in arguments")
 
  # I'm going to capture the status and timestamp and then check the see if
  # the timestamp is updated...
  oldstatus, oldtimestamp = statusstorage.read_status(vesseldict[vesselname]['statusfilename'])

  # Armon: this is required to fetch the networkrestrictions information from the configuration
  configuration = persist.restore_object("nodeman.cfg")

  
  # Armon: Generate the IP/Iface preferences if they exist
  ip_iface_preference_flags = []
  ip_iface_preference_str = ""    # Needed for Win Mobile

  # Only add the flags if everything necessary exists
  if 'networkrestrictions' in configuration and 'repy_restricted' in configuration['networkrestrictions'] \
    and configuration['networkrestrictions']['repy_restricted'] and 'repy_user_preference' in configuration['networkrestrictions']:
      # Generate the preference list
      for (is_ip, value) in configuration['networkrestrictions']['repy_user_preference']:
        # Append the correct flag
        if is_ip:
          ip_iface_preference_flags.append("--ip")
          ip_iface_preference_str += "--ip "
        else:
          ip_iface_preference_flags.append("--iface")
          ip_iface_preference_str += "--iface "
          
        # Append the value
        ip_iface_preference_flags.append(value)
        ip_iface_preference_str += "'" + value + "' "
        
      # Check for the --nootherips flag
      if 'repy_nootherips' in configuration['networkrestrictions'] and configuration['networkrestrictions']['repy_nootherips']:
        # Append the flag
        ip_iface_preference_flags.append("--nootherips")
        ip_iface_preference_str += "--nootherips "
    
  # Find the location where the sandbox files is located. Location of repyV1, repyV2 etc.
  prog_platform_location = os.path.join(prog_platform_dir[prog_platform], "repy.py")
 
  # I use absolute paths so that repy can still find the files after it 
  # changes directories...
  
  # Conrad: switched this to sequence-style Popen invocation so that spaces
  # in files work. Switched it back to absolute paths.
  command = [sys.executable, prog_platform_location] + ip_iface_preference_flags + [
      "--logfile", os.path.abspath(vesseldict[vesselname]['logfilename']),
      "--stop",    os.path.abspath(vesseldict[vesselname]['stopfilename']),
      "--status",  os.path.abspath(vesseldict[vesselname]['statusfilename']),
      "--cwd",     os.path.abspath(vesselname),
      "--servicelog", 
      "--execinfo",
      os.path.abspath(vesseldict[vesselname]['resourcefilename'])] + argstring.split()

  portable_popen.Popen(command)


  starttime = nonportable.getruntime()

  # wait for 10 seconds for it to start (else return an error)
  while nonportable.getruntime()-starttime < 10:
    newstatus, newtimestamp = statusstorage.read_status(vesseldict[vesselname]['statusfilename'])
    # Great!   The timestamp was updated...   The new status is the result of 
    # our work.   Let's tell the user what happened...
    if newtimestamp != oldtimestamp and newstatus != None:
      break

    

    # sleep while busy waiting...
    time.sleep(.5)

  else:
    return "Did not start in a timely manner\nWarning"

  # We need to update the status in the table because the status thread might
  # not notice this before our next request... (else occasional failures on XP)
  nmstatusmonitor.update_status(vesseldict, vesselname, newstatus, newtimestamp)


  return newstatus+"\nSuccess"




    

# Armon: Takes an optional exitparams tuple, which should contain
# an integer exit code and a string message,
# e.g. (44,'') is the default and represents exiting with status
# "Stopped" with no messege
def stopvessel(vesselname,exitparams=(44, '')):
  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"

  # this is broken out to prevent a race between checking the status and 
  # reporting the error
  currentstatus = vesseldict[vesselname]['status'] 
  # It must be started for us to stop it...
  if currentstatus != 'Started':
    raise BadRequest("Cannot stop vessel with status '"+currentstatus+"'")
  
  # Armon: Create the stop file, using a .tmp extension
  fileo = open(vesseldict[vesselname]['stopfilename']+".tmp","w")
  
  # Write out the stop string, Format: EINT;EMESG
  fileo.write(str(exitparams[0]) + ";" + exitparams[1])
  
  # Close the object
  fileo.close()
  
  # Rename the tmp file to the actual stopfile, this should be detected by repy now
  os.rename(vesseldict[vesselname]['stopfilename']+".tmp", vesseldict[vesselname]['stopfilename'])

  starttime = nonportable.getruntime()

  # wait for up to 10 seconds for it to stop (else return an error)
  while nonportable.getruntime()-starttime < 10:
    if vesseldict[vesselname]['status'] != 'Started':
      break

    # sleep while busy waiting...
    time.sleep(.5)

  else:
    return "May not have stopped in a timely manner\nWarning"
  
  return vesseldict[vesselname]['status']+"\nSuccess"
  


# Create a file in the vessel
def addfiletovessel(vesselname,filename, filedata):
  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"

  # The user is restricted from using filename starting with 'private_' since
  # this part of the namespace is reserved for custom security layers.
  if filename.startswith("private_"):
    raise BadRequest("User is not allowed to use file names starting with 'private_'")

  # get the current amount of data used by the vessel...
  currentsize = nonportable.compute_disk_use(vesselname+"/")
  # ...and the allowed amount
  resourcedict, call_list = resourcemanipulation.read_resourcedict_from_file(vesseldict[vesselname]['resourcefilename'])

  # If the current size + the size of the new data is too large, then deny
  if currentsize + len(filedata) > resourcedict['diskused']:
    raise BadRequest("Not enough free disk space")
  
  try:
    check_repy_filename(filename)
  except RepyArgumentError, e:
    raise BadRequest(str(e))
    
  if filename=="":
    raise BadRequest("Filename is empty")

  writefo = open(vesselname+"/"+filename,"w")
  writefo.write(filedata)
  writefo.close()

  return "\nSuccess"
  


# Return the list of files from the vessel
def listfilesinvessel(vesselname):
  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"

  # the directory should exist.   If not, it's an Internal error...
  filelist = os.listdir(vesselname+"/")
  filelist.sort()

  # remove any files in the protect part of the namespace
  filteredfilelist = []
  for filename in filelist:
    if not filename.startswith("private_"):
      filteredfilelist.append(filename)

  # return the list of files, separated by spaces
  return ' '.join(filteredfilelist) + "\nSuccess"
  


# Return a file from the vessel
def retrievefilefromvessel(vesselname,filename):
  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"

  if filename.startswith("private_"):
    raise BadRequest("User is not allowed to use file names starting with 'private_'")

  try:
    check_repy_filename(filename)
  except RepyArgumentError, e:
    raise BadRequest(str(e))

  try:
    readfo = open(vesselname+"/"+filename)
  except IOError, e:
    # file not found!   Let's detect and re-raise
    if e[0] == 2:
      return "Error, File Not Found\nError"
    
    # otherwise re-raise the error
    raise

  filedata = readfo.read()
  readfo.close()

  return filedata + "\nSuccess"
  


# Delete a file in the vessel
def deletefileinvessel(vesselname,filename):
  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"
  
  if filename.startswith("private_"):
    raise BadRequest("User is not allowed to use file names starting with 'private_'")

  try:
    check_repy_filename(filename)
  except RepyArgumentError, e:
    raise BadRequest(str(e))

  if not os.path.exists(vesselname+"/"+filename):
    raise BadRequest("File not found")

  os.remove(vesselname+"/"+filename)

  return "\nSuccess"
  

# Read the log file for the vessel
def readvessellog(vesselname):
  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"
  # copy the files, read the files, delete the copies.   
  # BUG: I don't believe there is a way to do this without any possibility for
  # race conditions (since copying two files is not atomic) without modifying
  # the sandbox to coordinate locking with the node manager
  

  # I'll use this to track if it fails or not.   This flag is used (instead of
  # doing the actual work) to minimize the time between copy calls.
  firstOK=False
  try:
    shutil.copyfile(vesseldict[vesselname]['logfilename']+'.old', "tmplog")
  except IOError, e:
    if e[0] == 2:
      # No such file or directory, we should ignore (we likely interrupted an 
      # non-atomic move)...
      pass
    else:
      raise
  else:
    firstOK = True


  secondOK = False
  # I have this next so that the amount of time between copying the files is 
  # minimized (I'll read both after)
  try:
    shutil.copyfile(vesseldict[vesselname]['logfilename']+'.new', "tmplog.new")
  except IOError, e:
    if e[0] == 2:
      # No such file or directory, we should ignore (we likely interrupted an 
      # non-atomic move)...
      pass
    else:
      raise
  else:
    secondOK = True


  # the log from the vessel
  readstring = ""

  # read the data and remove the files.
  if firstOK:
    readfo = open("tmplog")
    readstring = readstring + readfo.read()
    readfo.close()
    os.remove("tmplog")
    
  # read the data and remove the files.
  if secondOK:
    readfo = open("tmplog.new")
    readstring = readstring + readfo.read()
    readfo.close()
    os.remove("tmplog.new")

  # return only the last 16KB (hide the fact more may be stored)
  # NOTE: Should we return more?   We have more data...
  return readstring[-logmaxbuffersize:]+"\nSuccess"
  



# Flush a vessel's log, filesystem, and stop any running code...
# Armon: See stopvessel for explaination of exitparams
def resetvessel(vesselname,exitparams=(44, '')):
  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"

  # need to try to stop it until it works...
  while True:
    try:
      returnstring = stopvessel(vesselname,exitparams)
    except BadRequest:
      # due to the vessel not running...
      break

    # if we successfully stopped it, done...
    if returnstring.endswith('Success'):
      break

  # Okay, it is stopped now.   Now I'll clean up the file system...
  filelist = os.listdir(vesselname+"/")

  # don't delete any files in the protect part of the namespace
  for filename in filelist:
    if not filename.startswith("private_"):
      os.remove(vesselname+"/"+filename)

  # and remove the log files and stop file...
  if os.path.exists(vesseldict[vesselname]['logfilename']):
    os.remove(vesseldict[vesselname]['logfilename'])
  if os.path.exists(vesseldict[vesselname]['logfilename']+".new"):
    os.remove(vesseldict[vesselname]['logfilename']+".new")
  if os.path.exists(vesseldict[vesselname]['logfilename']+".old"):
    os.remove(vesseldict[vesselname]['logfilename']+".old")

  if os.path.exists(vesseldict[vesselname]['stopfilename']):
    os.remove(vesseldict[vesselname]['stopfilename'])

  if os.path.exists(vesseldict[vesselname]['stopfilename']):
    os.remove(vesseldict[vesselname]['stopfilename'])

  # change the status to Fresh
  statusstorage.write_status('Fresh',vesseldict[vesselname]['statusfilename'])

  # We need to update the status in the table because the status thread might
  # not notice this before the next request.
  nmstatusmonitor.update_status(vesseldict, vesselname, 'Fresh', time.time())

  return "\nSuccess"


def changeowner(vesselname, newkeystring):
  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"
  try:
    newkey = rsa_string_to_publickey(newkeystring)
  except ValueError:
    raise BadRequest("Invalid Key String")
    
  # check the key 
  if not rsa_is_valid_publickey(newkey):
    raise BadRequest("Invalid Key")

  vesseldict[vesselname]['ownerkey'] = newkey 

  # Must reset the owner information because it's used for service security.
  vesseldict[vesselname]['ownerinformation'] = ''

  # Reset the advertise flag so the owner can find the node...
  vesseldict[vesselname]['advertise'] = True

  persist.commit_object(vesseldict, "vesseldict")
  return "\nSuccess"
  

def changeusers(vesselname, listofkeysstring):
  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"
  newkeylist = []

  # check the keys
  for keystring in listofkeysstring.split('|'):
    if keystring == '':
      continue

    try:
      newkey = rsa_string_to_publickey(keystring)
    except ValueError:
      raise BadRequest("Invalid Key String '"+keystring+"'")

    if not rsa_is_valid_publickey(newkey):
      raise BadRequest("Invalid Key '"+keystring+"'")
    
    newkeylist.append(newkey)

  vesseldict[vesselname]['userkeys'] = newkeylist
    
  persist.commit_object(vesseldict, "vesseldict")
  return "\nSuccess"

def changeownerinformation(vesselname, ownerstring):
  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"
  if len(ownerstring) > maxownerstringlength:
    raise BadRequest("String Too Long")

  if '\n' in ownerstring:
    raise BadRequest("String may not contain newline character")

  vesseldict[vesselname]['ownerinformation'] = ownerstring

  persist.commit_object(vesseldict, "vesseldict")
  return "\nSuccess"
  


# NOTE: Should this be something other than True / False (perhaps owner / user /
# none?)
def changeadvertise(vesselname, setting):
  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"
  if setting == 'True':
    vesseldict[vesselname]['advertise'] = True
  elif setting == 'False':
    vesseldict[vesselname]['advertise'] = False
  else: 
    raise BadRequest("Invalid advertisement setting '"+setting+"'")

  persist.commit_object(vesseldict, "vesseldict")
  return "\nSuccess"


lastusednumber = None

# private.   A helper function that returns an unused vessel name
def get_new_vessel_name():
  global lastusednumber
  if lastusednumber == None:
    # let's look at the dictionary and figure something out
    maxval = 0
    for vesselname in vesseldict:
      # I'm assuming naming is done by 'v'+number
      assert(vesselname[0] == 'v')
      maxval = max(maxval, int(vesselname[1:]))
  
    lastusednumber = maxval

  lastusednumber = lastusednumber + 1
  return 'v'+str(lastusednumber)




# Private.   Creates a new vessel's state in the dictionary and on disk
def _setup_vessel(vesselname, examplevessel, resourcedict, call_list):
  if vesselname in vesseldict:
    raise Exception, "Internal Error, setting up vessel '"+vesselname+"' already in vesseldict"

  # write the new resource file
  resourcemanipulation.write_resourcedict_to_file(resourcedict, 'resource.'+vesselname, call_list)

  # Set the invariants up...
  item = {}
  item['stopfilename'] = vesselname+".stop"
  item['logfilename'] = vesselname+".log"
  item['resourcefilename'] = "resource."+vesselname
  item['status'] = 'Fresh'
  item['statusfilename'] = vesselname+".status"

  # first the easy stuff...   Set up the vesseldict dictionary
  if examplevessel == None:
    item['userkeys'] = []
    item['ownerkey'] = {}
    item['oldmetadata'] = None
    item['advertise'] = True
    item['ownerinformation'] = ''

  else:
    if examplevessel not in vesseldict:
      raise Exception("Internal Error, examplevessel '"+examplevessel+"' not in vesseldict")

    item['userkeys'] = vesseldict[examplevessel]['userkeys']
    item['ownerkey'] = vesseldict[examplevessel]['ownerkey']
    item['oldmetadata'] = vesseldict[examplevessel]['oldmetadata']
    item['advertise'] = vesseldict[examplevessel]['advertise']
    item['ownerinformation'] = vesseldict[examplevessel]['ownerinformation']

  # create the directory on the file system
  os.mkdir(vesselname)

  # now we're ready to add the entry to the table (so other threads can use it)
  vesseldict[vesselname] = item


    

# Private
# BUG: What about a running vessel?
def _destroy_vessel(vesselname):
  if vesselname not in vesseldict:
    raise Exception, "Internal Error, destroying a non-existant vessel '"+vesselname+"'"

  # remove the entry first so other threads aren't confused
  item = vesseldict[vesselname]
  del vesseldict[vesselname]


  shutil.rmtree(vesselname)
  if os.path.exists(item['logfilename']):
    os.remove(item['logfilename'])
  if os.path.exists(item['logfilename']+".new"):
    os.remove(item['logfilename']+".new")
  if os.path.exists(item['logfilename']+".old"):
    os.remove(item['logfilename']+".old")

  if os.path.exists(item['stopfilename']):
    os.remove(item['stopfilename'])

  if os.path.exists(item['resourcefilename']):
    os.remove(item['resourcefilename'])



def splitvessel(vesselname, resourcedata):
  if vesselname not in vesseldict:
    raise BadRequest, "No such vessel"
  
  if vesseldict[vesselname]['status'] == 'Started':
    raise BadRequest("Attempting to split a running vessel")

  # get the new name
  newname1 = get_new_vessel_name()
  newname2 = get_new_vessel_name()

  try:
    proposedresourcedict, call_list = resourcemanipulation.parse_resourcedict_from_string(resourcedata)
  except resourcemanipulation.ResourceParseError, e:
    raise BadRequest(str(e))
  
  # we must have enough so that starting - offcut - proposed > 0
  startingresourcedict, call_list_vessel = resourcemanipulation.read_resourcedict_from_file(vesseldict[vesselname]['resourcefilename'])

  offcutresourcedict, offcut_call_list = resourcemanipulation.read_resourcedict_from_file(offcutfilename)

  # let's see what happens if we just remove the offcut...
  try:
    intermediateresourcedict = resourcemanipulation.subtract_resourcedicts(startingresourcedict,offcutresourcedict)

  except resourcemanipulation.ResourceMathError, e:
    raise BadRequest('Existing resources are so small they cannot be split.\n'+str(e))

  # now let's remove the proposed bits!
  try:
    finalresourcedict = resourcemanipulation.subtract_resourcedicts(intermediateresourcedict,proposedresourcedict)
  except resourcemanipulation.ResourceMathError, e:
    raise BadRequest('Proposed vessel is too large.\n'+str(e))

  # newname1 becomes the leftovers...
  _setup_vessel(newname1, vesselname, finalresourcedict, call_list_vessel)
  # newname2 is what the user requested
  _setup_vessel(newname2, vesselname, proposedresourcedict, call_list_vessel)
  _destroy_vessel(vesselname)
    
  persist.commit_object(vesseldict, "vesseldict")
  return newname1+" "+newname2+"\nSuccess"

    
  
  

def joinvessels(vesselname1, vesselname2):
  if vesselname1 not in vesseldict:
    raise BadRequest, "No such vessel '"+vesselname1+"'"
  if vesselname2 not in vesseldict:
    raise BadRequest, "No such vessel '"+vesselname2+"'"

  if vesseldict[vesselname1]['ownerkey'] != vesseldict[vesselname2]['ownerkey']:
    raise BadRequest("Vessels must have the same owner")
  
  if vesseldict[vesselname1]['status'] == 'Started' or vesseldict[vesselname2]['status'] == 'Started':
    raise BadRequest("Attempting to join a running vessel")
  
  # get the new name
  newname = get_new_vessel_name()


  currentresourcedict1, call_list_v1 = resourcemanipulation.read_resourcedict_from_file(vesseldict[vesselname1]['resourcefilename'])
  currentresourcedict2, call_list_v2 = resourcemanipulation.read_resourcedict_from_file(vesseldict[vesselname2]['resourcefilename'])
  offcutresourcedict, offcut_call_list = resourcemanipulation.read_resourcedict_from_file(offcutfilename)


  # the final resources are reconstructed from the offcut + vessel1 + vessel2
  intermediateresourcedict = resourcemanipulation.add_resourcedicts(currentresourcedict1, currentresourcedict2)

  finalresourcedict = resourcemanipulation.add_resourcedicts(intermediateresourcedict, offcutresourcedict)

  # MMM: We add in the call list of resources from one of the original 
  # resource file. Since this is for backward compatibility for Repy V1,
  # it does not matter which call list we add in to the final resource file.
  _setup_vessel(newname, vesselname1, finalresourcedict, call_list_v1)
  _destroy_vessel(vesselname1)
  _destroy_vessel(vesselname2)
    
  persist.commit_object(vesseldict, "vesseldict")
  return newname+"\nSuccess"

    

# JAC: note that setrestrictions is obsolete...
#def setrestrictions(vesselname, restrictionsdata):
