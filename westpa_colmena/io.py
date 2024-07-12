"""HDF5 I/O for WESTPA simulations."""

from __future__ import annotations

import h5py
import numpy as np
from pydantic import BaseModel
from pydantic import Field

import westpa_colmena
from westpa_colmena.ensemble import BasisStates
from westpa_colmena.ensemble import SimMetadata
from westpa_colmena.ensemble import TargetState


class DDWEMetadata(BaseModel):
    """Metadata for the weighted ensemble."""

    west_current_iteration: int = Field(
        ...,
        description='The current iteration of the simulation.',
    )
    west_file_format_version: int = Field(
        9,
        description='The version of the file format.',
    )
    west_iter_prec: int = Field(
        8,
        description='The number of padding zeros for iteration number.',
    )
    west_version: str = Field(
        westpa_colmena.__version__,
        description='The version of WESTPA.',
    )


# Define data types for use in the HDF5 file

# Up to 9 quintillion segments per iteration;
# signed so that initial states can be stored negative
seg_id_dtype = np.int64
# Up to 4 billion iterations
n_iter_dtype = np.uint32
# About 15 digits of precision in weights
weight_dtype = np.float64
# ("u" for Unix time) Up to ~10^300 cpu-seconds
utime_dtype = np.float64
# Variable-length string
vstr_dtype = h5py.special_dtype(vlen=str)
# Reference to an HDF5 object
h5ref_dtype = h5py.special_dtype(ref=h5py.Reference)
# Hash of a binning scheme
binhash_dtype = np.dtype('|S64')

summary_table_dtype = np.dtype(
    [
        # Number of live trajectories in this iteration
        ('n_particles', seg_id_dtype),
        # Norm of probability, to watch for errors or drift
        ('norm', weight_dtype),
        # Per-bin minimum probability
        ('min_bin_prob', weight_dtype),
        # Per-bin maximum probability
        ('max_bin_prob', weight_dtype),
        # Per-segment minimum probability
        ('min_seg_prob', weight_dtype),
        # Per-segment maximum probability
        ('max_seg_prob', weight_dtype),
        # Total CPU time for this iteration
        ('cputime', utime_dtype),
        # Total wallclock time for this iteration
        ('walltime', utime_dtype),
        # Hash of the binning scheme used in this iteration
        ('binhash', binhash_dtype),
    ],
)

# Index to basis/initial states
ibstate_index_dtype = np.dtype(
    [
        # Iteration when this state list is valid
        ('iter_valid', np.uint),
        # Number of basis states
        ('n_bstates', np.uint),
        # Reference to a group containing further data
        ('group_ref', h5ref_dtype),
    ],
)

# Basis state index type
bstate_dtype = np.dtype(
    [
        # An optional descriptive label
        ('label', vstr_dtype),
        # Probability that this state will be selected
        ('probability', weight_dtype),
        # An optional auxiliary data reference
        ('auxref', vstr_dtype),
    ],
)

tstate_index_dtype = np.dtype(
    [
        # Iteration when this state list is valid
        ('iter_valid', np.uint),
        # Number of target states
        ('n_states', np.uint),
        # Reference to a group containing further data; this will be the
        ('group_ref', h5ref_dtype),
    ],
)

# Null reference if there is no target state for that timeframe.
# An optional descriptive label for this state
tstate_dtype = np.dtype([('label', vstr_dtype)])

# Storage of bin identities
binning_index_dtype = np.dtype(
    [('hash', binhash_dtype), ('pickle_len', np.uint32)],
)


class WestpaH5File:
    """Utility class for writing WESTPA HDF5 files."""

    def __init__(self, h5file: str, config: DDWEMetadata) -> None:
        self.config = config
        self.h5file = h5file

        # Create the file
        with h5py.File(h5file, mode='w') as f:
            # Set attribute metadata
            f.attrs[
                'west_file_format_version'
            ] = config.west_file_format_version
            f.attrs['west_iter_prec'] = config.west_iter_prec
            f.attrs['west_version'] = config.west_version
            f.attrs['westpa_iter_prec'] = config.west_iter_prec
            f.attrs[
                'westpa_fileformat_version'
            ] = config.west_file_format_version

            # Create the summary table
            f.create_dataset(
                'summary',
                shape=(1,),
                dtype=summary_table_dtype,
                maxshape=(None,),
            )

            # Create the iterations group
            f.create_group('iterations')

    def append_summary(
        self,
        h5_file: h5py.File,
        n_iter: int,
        next_iteration: list[SimMetadata],
        binned_sims: list[list[SimMetadata]],
    ) -> None:
        """Create a row for the summary table."""
        # Create a row for the summary table
        summary_row = np.zeros((1,), dtype=summary_table_dtype)
        # The number of simulation segments in this iteration
        summary_row['n_particles'] = len(next_iteration)
        # Compute the total weight of all segments (should be close to 1.0)
        summary_row['norm'] = sum(x.weight for x in next_iteration)
        # Compute the min and max weight over all segments
        summary_row['min_seg_prob'] = min(x.weight for x in next_iteration)
        summary_row['max_seg_prob'] = max(x.weight for x in next_iteration)
        # Compute the min and max weight of each bin
        summary_row['min_bin_prob'] = min(
            sum(x.weight for x in sims) for sims in binned_sims
        )
        summary_row['max_bin_prob'] = max(
            sum(x.weight for x in sims) for sims in binned_sims
        )

        # TODO: Set the cputime which measures the total CPU time for
        # this iteration
        summary_row['cputime'] = 0.0
        # TODO: Set the walltime which measures the total wallclock time
        # for this iteration
        summary_row['walltime'] = 0.0

        # Save a hex string identifying the binning used in this iteration
        summary_row['binhash'] = next_iteration[0].binner_hash

        # Create a table of summary information about each iteration
        summary_table = h5_file['summary']

        # Resize the summary table if necessary
        if len(summary_table) < n_iter:
            summary_table.resize((n_iter + 1,))

        # Update the summary table
        summary_table[n_iter - 1] = summary_row

    def append_ibstates(
        self,
        h5_file: h5py.File,
        n_iter: int,
        basis_states: BasisStates,
    ) -> None:
        """Append the initial basis states to the HDF5 file."""
        # Create the group used to store basis states and initial states
        group = h5_file.require_group('ibstates')

        # Check if 'index' dataset exists in group
        if 'index' in group:
            # Resize the index dataset to add a new row
            index = group['index']
            index.resize((len(index) + 1,))
        else:
            # Create the index dataset if it does not exist
            index = group.create_dataset(
                'index',
                dtype=ibstate_index_dtype,
                shape=(1,),
                maxshape=(None,),
            )

        # Create a new row for the index dataset
        set_id = len(index) - 1
        index_row = index[set_id]
        index_row['iter_valid'] = n_iter
        index_row['n_bstates'] = len(basis_states)
        state_group = group.create_group(str(set_id))
        index_row['group_ref'] = state_group.ref

        if basis_states:
            # Create the basis state table
            state_table = np.empty((len(basis_states),), dtype=bstate_dtype)

            # Populate the state table
            for i, state in enumerate(basis_states):
                state_table[i]['label'] = str(state.simulation_id)
                state_table[i]['probability'] = state.weight
                state_table[i]['auxref'] = state.auxref

            # Get the pcoords for the basis states
            state_pcoords = np.array([x.parent_pcoord for x in basis_states])

            # Add the basis state table to the state group
            state_group['bstate_index'] = state_table
            state_group['bstate_pcoord'] = state_pcoords

        # Update the index dataset
        index[set_id] = index_row

    def append_tstates(
        self,
        h5_file: h5py.File,
        n_iter: int,
        target_states: list[TargetState],
    ) -> None:
        """Append the target states to the HDF5 file."""
        # Create the group used to store target states
        group = h5_file.require_group('tstates')

        if 'index' in group:
            # Resize the index dataset to add a new row
            index = group['index']
            index.resize((len(index) + 1,))
        else:
            # Create the index dataset if it does not exist
            index = group.create_dataset(
                'index',
                dtype=tstate_index_dtype,
                shape=(1,),
                maxshape=(None,),
            )

        # Create a new row for the index dataset
        set_id = len(index) - 1
        index_row = index[set_id]
        index_row['iter_valid'] = n_iter
        index_row['n_states'] = len(target_states)

        if target_states:
            # Collect the target state labels
            state_table = np.empty((len(target_states),), dtype=tstate_dtype)
            for i, state in enumerate(target_states):
                state_table[i]['label'] = state.label
            # Collect the pcoords for the target states
            state_pcoords = np.array([x.pcoord for x in target_states])

            # Create the group for the target states
            state_group = group.create_group(str(set_id))

            # Add the target state table to the state group
            index_row['group_ref'] = state_group.ref
            state_group['index'] = state_table
            state_group['pcoord'] = state_pcoords

        else:
            index_row['group_ref'] = None

        # Update the index dataset
        index[set_id] = index_row

    def append_bin_mapper(self, h5_file: h5py.File, sim: SimMetadata) -> None:
        """Append the bin mapper to the HDF5 file."""
        # Create the group used to store bin mapper
        group = h5_file.require_group('/bin_topologies')

        # Extract the bin mapper data
        pickle_data = sim.binner_pickle
        hashval = sim.binner_hash

        if 'index' in group and 'pickles' in group:
            # Resize the index and pickle_ds datasets to add a new row
            index = group['index']
            pickle_ds = group['pickles']
            index.resize((len(index) + 1,))
            new_hsize = max(pickle_ds.shape[1], len(pickle_data))
            pickle_ds.resize((len(pickle_ds) + 1, new_hsize))
        else:
            # Create the index and pickle_ds datasets if they do not exist
            index = group.create_dataset(
                'index',
                shape=(1,),
                maxshape=(None,),
                dtype=binning_index_dtype,
            )
            pickle_ds = group.create_dataset(
                'pickles',
                dtype=np.uint8,
                shape=(1, len(pickle_data)),
                maxshape=(None, None),
                chunks=(1, 4096),
                compression='gzip',
                compression_opts=9,
            )

        # Populate the new row in the index dataset
        ind = len(index) - 1
        index_row = index[ind]
        index_row['hash'] = hashval
        index_row['pickle_len'] = len(pickle_data)

        # Update the index and pickle_ds datasets
        index[ind] = index_row
        pickle_ds[ind, : len(pickle_data)] = memoryview(pickle_data)

    def append(
        self,
        next_iteration: list[SimMetadata],
        binned_sims: list[list[SimMetadata]],
        basis_states: BasisStates,
        target_states: list[TargetState],
    ) -> None:
        """Append the next iteration to the HDF5 file."""
        # Make sure at least one simulation is provided
        if not next_iteration:
            raise ValueError('next_iteration must not be empty')

        # Get a sim metadata object to extract metadata from
        sim = next_iteration[0]
        # Ensure we have a list for guaranteed ordering
        n_iter = sim.iteration_id

        with h5py.File(self.h5file, mode='a') as f:
            # Append the summary table row
            self.append_summary(f, n_iter, next_iteration, binned_sims)

            # Append the basis states if we are on the first iteration
            if n_iter:
                self.append_ibstates(f, n_iter, basis_states)

            # Append the target states if we are on the first iteration
            if n_iter:
                self.append_tstates(f, n_iter, target_states)

            # Append the bin mapper if we are on the first iteration
            # NOTE: this assumes the binning scheme is constant
            if n_iter:
                self.append_bin_mapper(f, sim)

            # TODO: We may need to add istate_index, istate_pcoord into the
            #       ibstates group. But for now, we are not.

            # TODO: Note that in the westpa code the bin mapper append function
            #       also updates the iter_group.attrs['binhash'] field within
            #       the iterations/iter_ group.

            # Create the iteration group
            iter_group = f.require_group(
                '/iterations/iter_{:0{prec}d}'.format(
                    int(n_iter),
                    prec=self.config.west_iter_prec,
                ),
            )
            iter_group.attrs['n_iter'] = n_iter

            # TODO: Once we are finished implementing each component,
            #       revisit the westpa analog of this function to make
            #       sure everything is complete.
