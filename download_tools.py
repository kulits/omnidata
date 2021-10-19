import aria2p
import multiprocess as mp
import atexit, os, signal, shutil, tempfile, time
from   subprocess import call, run, Popen
import tarfile 
from   typing import Optional

from metadata import ZippedModel, bcolors, notice, header
from starter_dataset import STARTER_DATASET_REMOTE_SERVER_METADATAS, STARTER_DATA_COMPONENT_TO_SPLIT, STARTER_DATA_COMPONENT_TO_SUBSET, STARTER_DATA_COMPONENTS
from fastcore.script import *

### Interaction w/ remote server

def log_parameters(metadata_list, domains, subset, split, components, dest, dest_compressed, **kwargs):
  header('-------------------------------------')
  header(f'From {bcolors.OKGREEN}SERVERS{bcolors.ENDC}:')
  for rsm in metadata_list: header(f'    {bcolors.UNDERLINE}{rsm.link_file}{bcolors.ENDC}')
  header('')
  header(f'Data {bcolors.OKGREEN}parameters{bcolors.ENDC}: (what to download)') 
  header(f'    {bcolors.WARNING}Domains{bcolors.ENDC}    = {domains}') 
  header(f'    {bcolors.WARNING}Components{bcolors.ENDC} = {components}') 
  header(f'    {bcolors.WARNING}Subset{bcolors.ENDC}     = {subset}') 
  header(f'    {bcolors.WARNING}Split{bcolors.ENDC}      = {split}') 
  header('')
  header(f'Data {bcolors.OKGREEN}locations{bcolors.ENDC}:') 
  header(f'    {bcolors.WARNING}Dataset (extracted){bcolors.ENDC}      = {dest}') 
  header(f'    {bcolors.WARNING}Compressed files   {bcolors.ENDC}      = {dest_compressed}') 
  header('-------------------------------------')
  # print(f'[{bcolors.OKGREEN}FETCHING{bcolors.ENDC}] metadata from:')

def end_notes(**kwargs):
  notice(f'[{bcolors.OKGREEN + bcolors.BOLD}Download complete{bcolors.ENDC}]')
  notice('    Number of model files downloaded={}')
  notice('Recap:')
  log_parameters(**kwargs))

def validate_checksums_exist(models):
  models_without_checksum = [m for m in models if m.checksum is None]
  if len(models_without_checksum) > 0: 
    show_k = 100
    print(f'Found {len(models_without_checksum)} models without checksums:')
    for m in models_without_checksum[:show_k]: print(f'    {m.url}')
    if len(models_without_checksum) > show_k:  print(f'    and {len(models_without_checksum) - show_k} more...')
    print(f'Since "--ignore_checksum=False", cannot continue. Aborting.')
    exit(1)
##


### Downloading
def ensure_aria2_server(aria2_create_server, aria2_uri, aria2_secret, connections_total, connections_per_server_per_download, aria2_cmdline_opts, **kwargs):
  if not aria2_uri or not aria2_create_server: return None
  a2host, a2port = ":".join(aria2_uri.split(':')[:-1]), aria2_uri.split(':')[-1]
  notice(f"Opening aria2c download daemon in background: {bcolors.WARNING}Run {bcolors.OKCYAN}'aria2p'{bcolors.WARNING} in another window{bcolors.ENDC} to view status.") 
  n = connections_total 
  x = connections_per_server_per_download if connections_per_server_per_download is not None else connections_total
  x = min(x, 16)
  a2server = Popen(('aria2c --enable-rpc --rpc-listen-all -c --auto-file-renaming=false ' +
                    '--optimize-concurrent-downloads ' + 
                    f'-s{n}  -j{n}  -x{x} {aria2_cmdline_opts}').split())
  atexit.register(os.kill, a2server.pid, signal.SIGINT)
  return aria2p.API(aria2p.Client(host=a2host, port=a2port, secret=aria2_secret))


def download_tar(url, output_dir='.', output_name=None, n=20, n_per_server=10,
  checksum=None, max_tries_per_model=3, aria2api=None, dryrun=False,
  ) -> Optional[str]:
  '''Downloads "url" to output filename. Returns downloaded fpath.'''
  fname = url.split('/')[-1] if output_name is None else output_name
  fpath = os.path.join(output_dir, fname)
  if dryrun: print(f'Downloading "{url}"" to "{fpath}"'); return fpath
  # checksum = checksum[:-3] + '000'
  # print(checksum)
  if aria2api is not None:
    while (max_tries_per_model := max_tries_per_model-1) > 0:
      res = aria2api.client.add_uri(uris=[url], options={
        'out': fname, 'dir': output_dir,
        'check_integrity': True, 'checksum': checksum
        })
      success = wait_on(aria2api, res)
      if success: break
    if not success: return None
  else:
    options = f'-c --auto-file-renaming=false'
    options += f' -s {n} -j {n} -x {n_per_server}' # N connections
    if checksum is not None: options += f' --check-integrity=true --checksum={checksum}'
    cmd = f'aria2c -d {output_dir} -o {fname} {options} "{url}"'
    call(cmd, shell=True)
  return fpath

def wait_on(a2api, gid, duration=0.2):
  while not (a2api.get_downloads([gid])[0].is_complete or a2api.get_downloads([gid])[0].has_failed):
    time.sleep(duration)
  success = a2api.get_downloads([gid])[0].is_complete 
  a2api.remove(a2api.get_downloads([gid]))
  return success
##

### Untarring
def untar(fpath, model, dest=None, ignore_existing=True,
    output_structure=('domain', 'component_name', 'model_name'), # Desired directory structure
    dryrun=False
  ) -> None:
  dest_fpath = os.path.join(dest, *[getattr(model, a) for a in output_structure])
  if dest is not None: os.makedirs(dest, exist_ok=True)
  if os.path.exists(dest_fpath) and ignore_existing: notice(f'"{dest_fpath}" already exists... skipping'); return
  with tempfile.TemporaryDirectory(dir=dest) as tmpdirname:
    src_fpath = os.path.join(tmpdirname, *[getattr(model, a) for a in model.tar_structure])
    if dryrun: print(f'Extracting "{fpath}"" to "{tmpdirname}" and moving "{src_fpath}" to "{dest_fpath}"'); return
    with tarfile.open(fpath) as tar:
      tar.extractall(path=tmpdirname)
    shutil.move(src_fpath, dest_fpath)

##

def filter_models(models, domains, subset, split, components, component_to_split, component_to_subset):
  return [m for m in models 
    if (components == 'all' or m.component_name in components)
    and (subset == 'all' or component_to_subset[m.component_name] is None or m.model_name in component_to_subset[m.component_name][subset]) 
    and (split == 'all' or component_to_split[m.component_name] is None or m.model_name in component_to_split[m.component_name])
    and (domains == 'all' or m.domain in domains)
    ]


@call_parse
def main(
  domains:     Param("Domains to download (comma-separated or 'all')", str, nargs='+'),
  subset:      Param("Subset to download", str, choices=['debug', 'tiny', 'medium', 'full', 'fullplus'])='debug',
  split:       Param("Split to download", str, choices=['train', 'val', 'test', 'all'])='all',
  components:  Param("Component datasets to download (comma-separated)", str, nargs='+',
    choices=['all','replica','taskonomy','gso_in_replica','hypersim','blendedmvs','hm3d','clevr_simple','clevr_complex'])='all',
  dest:             Param("Where to put the uncompressed data", str)='uncompressed/',
  dest_compressed:  Param("Where to download the compressed data", str)='compressed/',
  keep_compressed:  Param("Don't delete compressed files after decompression", bool_arg)=False,
  only_download:    Param("Only download compressed data", bool_arg)=False,
  max_tries_per_model:    Param("Number of times to try to download model if checksum fails.", int)=3,  
  connections_total:      Param("Number of simultaneous aria2c connections overall (note: if not using the RPC server, this is per-worker)", int)=8,
  connections_per_server_per_download: Param("Number of simulatneous aria2c connections per server per download. Defaults to 'total_connections' (note: if not using the RPC server, this is per-worker)", int)=None,
  n_workers:              Param("Number of workers to use", int)=mp.cpu_count(),
  num_chunk:        Param("Download the kth slice of the overall dataset", int)=0,
  num_total_chunks: Param("Download the dataset in N total chunks. Use with '--num_chunk'", int)=1, 
  ignore_checksum:  Param("Ignore checksum validation", bool_arg)=False,
  dryrun:           Param("Keep compressed files even after decompressing", store_true)=False,
  aria2_uri:              Param("Location of aria2c RPC (if None, use CLI)", str)="http://localhost:6800", 
  aria2_cmdline_opts:     Param("Opts to pass to aria2c", str)='',  
  aria2_create_server:    Param("Create a RPC server at aria2_uri", bool_arg)=True, 
  aria2_secret:           Param("Secret for aria2c RPC", str)='', 
  ):
  ''' 
    Downloads Omnidata starter dataset.
    ---
    The data is stored on the remote server in a compressed format (.tar.gz).
    This function downloads the compressed and decompresses it.

    Examples:
      python download_tools.py rgb normals point_info --components clevr_simple clevr_complex --connections_total 30
  '''
  metadata_list=STARTER_DATASET_REMOTE_SERVER_METADATAS
  log_parameters(**locals())
  aria2 = ensure_aria2_server(**locals())

  # Determine which models to use
  models = [metadata.parse(url)
            for metadata in STARTER_DATASET_REMOTE_SERVER_METADATAS
            for url in metadata.links]
  models = filter_models(models, domains, subset, split, components, 
            component_to_split=STARTER_DATA_COMPONENT_TO_SPLIT,
            component_to_subset=STARTER_DATA_COMPONENT_TO_SUBSET)
  models = models[num_chunk::num_total_chunks] # Parallelization: striped slice of models array
  if ignore_checksum: validate_checksums_exist(models)


  # Process download
  def process_model(model):
    tar_fpath = download_tar(
                  model.url, output_dir=dest_compressed, output_name=model.fname, 
                  checksum=f'md5={model.checksum}', aria2api=aria2, dryrun=dryrun)
    if tar_fpath is None: return
    if only_download:     return
    untar(tar_fpath, dest=dest, model=model, ignore_existing=True, dryrun=dryrun)
    if not keep_compressed: os.remove(tar_fpath)

  if n_workers <=1 : [process_model(model) for model in models]
  else:
    with mp.Pool(n_workers) as p:
      p.map(process_model, models)

  # Cleanup

  pass


if __name__ == '__main__':
  a2server = Popen('aria2c --enable-rpc --rpc-listen-all -c --auto-file-renaming=false -s 10 -x 10'.split())

  time.sleep(0.2)
  model = ZippedModel(
    component_name='taskonomy', domain='point_info', model_name='yscloskey', 
    url='https://datasets.epfl.ch/taskonomy/yscloskey_point_info.tar'
  )
  tar_format = ('domain',)
  dest_compressed = '/tmp/omnidata/compressed'
  dest = '/tmp/omnidata/uncompressed'

  tar_fpath = download_tar(model.url, output_dir=dest_compressed,
    checksum='md5=9f9752d74b07bcc164af4a6c61b0eca1',
    output_name=model.fname)
  untar(tar_fpath, dest=dest, model=model, tar_format=tar_format, ignore_existing=True)
  os.remove(tar_fpath)

  # Terminate the process
  os.kill(a2server.pid, signal.SIGINT)
