"""
.. module:: data
   :synopsis: Define the Data and Parameter classes

.. moduleauthor:: Benjamin Audren <benjamin.audren@epfl.ch>
"""
import os
import sys
import math
import random as rd
import warnings
import subprocess as sp
import re

import io_mp  # Needs to talk to io_mp.py file for the logging
                               # of parameters
import prior

# A modified version of Python dictionary in order to keep track of the order
# in it (much the same as in an array). In case an older version of Python is
# used, this module does not belong to collections. Please remind to put that
# into your PYTHONPATH variable.
try:
    from collections import OrderedDict as od
except ImportError:
    try:
        from ordereddict import OrderedDict as od
    except ImportError:
        raise io_mp.MissingLibraryError(
            "If you are running with Python v2.5 or 2.6, you need" +
            "to manually install the ordereddict package by placing" +
            "the file ordereddict.py in your Python Path")


class Data(object):
    """
    Store all relevant data to communicate between the different modules.

    """

    def __init__(self, command_line, path):
        """
        The Data class holds the cosmological information, the parameters from
        the MCMC run, the information coming from the likelihoods. It is a wide
        collections of information, with in particular two main dictionaries:
        cosmo_arguments and mcmc_parameters.

        It defines several useful **methods**. The following ones are called
        just once, at initialization:

        * :func:`fill_mcmc_parameters`
        * :func:`read_file`
        * :func:`read_version`
        * :func:`group_parameters_in_blocks`

        On the other hand, these two following functions are called every step.

        * :func:`check_for_slow_step`
        * :func:`update_cosmo_arguments`

        Finally, the convenient method :func:`get_mcmc_parameters` will be
        called in many places, to return the proper list of desired parameters.

        It has a number of different **attributes**, and the more important
        ones are listed here:

        * :attr:`boundary_loglike`
        * :attr:`cosmo_arguments`
        * :attr:`mcmc_parameters`
        * :attr:`need_cosmo_update`
        * :attr:`log_flag`

        .. note::

            The `experiments` attribute is extracted from the parameter file,
            and contains the list of likelihoods to use

        .. note::

            The path argument will be used in case it is a first run, and hence
            a new folder is created. If starting from an existing folder, this
            dictionary will be compared with the one extracted from the
            log.param, and will use the latter while warning the user.

        .. warning::

            New in version 2.0.0, you can now specify an oversampling of the
            nuisance parameters, to hasten the execution of a run with
            likelihoods that have many of them. You should specify a new field
            in the parameter file, `data.over_sampling = [1, ...]`, that
            contains a 1 on the first element, and then the over sampling of
            the desired likelihoods. This array must have the same size as the
            number of blocks (1 for the cosmo + 1 for each likelihood with
            varying nuisance parameters). You need to call the code with the
            flag `-j jast` for it to be used.

        To create an instance of this class, one must feed the following
        parameters and keyword arguments:

        Parameters
        ----------
        command_line : NameSpace
            NameSpace containing the input from the :mod:`parser_mp`. It
            stores the input parameter file, the jumping methods, the output
            folder, etc...  Most of the information extracted from the
            command_file will be transformed into :class:`Data` attributes,
            whenever it felt meaningful to do so.
        path : dict
            Contains a dictionary of important local paths. It is used here to
            find the cosmological module location.

        """

        # Initialisation of the random seed
        rd.seed()

        # Store the parameter file
        self.param = command_line.param

        # Recover jumping method from command_line
        self.jumping = command_line.jumping
        self.jumping_factor = command_line.jumping_factor

        # Initialise the path dictionnary.
        self.path = {}

        self.boundary_loglike = -1e30
        """
        Define the boundary loglike, the value used to defined a loglike
        that is out of bounds. If a point in the parameter space is affected to
        this value, it will be automatically rejected, hence increasing the
        multiplicity of the last accepted point.
        """

        # Creation of the two main dictionnaries:
        self.cosmo_arguments = {}
        """
        Simple dictionary that will serve as a communication interface with the
        cosmological code. It contains all the parameters for the code that
        will not be set to their default values.  It is updated from
        :attr:`mcmc_parameters`.

        :rtype:   dict
        """
        self.mcmc_parameters = od()
        """
        Ordered dictionary of dictionaries, it contains everything needed by
        the :mod:`mcmc` module for the MCMC procedure.  Every parameter name
        will be the key of a dictionary, containing the initial configuration,
        role, status, last accepted point and current point.

        :rtype: ordereddict
        """

        # Arguments for PyMultiNest
        self.NS_arguments = {}
        """
        Dictionary containing the parameters needed by the PyMultiNest sampler.
        It is filled just before the run of the sampler.  Those parameters not
        defined will be set to the default value of PyMultiNest.

        TODO: ADD PROPER REFERENCES TO DOCUMENTATION.

        :rtype: dict
        """

        # Initialise the experiments attribute
        self.experiments = []

        # Initialise the oversampling setting
        self.over_sampling = []
        """
        List storing the respective over sampling of the parameters. The first
        entry, applied to the cosmological parameters, will always be 1.
        Setting it to anything else would simply rescale the whole process. If
        not specified otherwise in the parameter file, all other numbers will
        be set to 1 as well.

        :rtype: list
        """

        # Default value for the number of steps
        self.N = 10

        # Create the variable out, and out_name, which will be initialised
        # later by the :mod:`io_mp` module
        self.out = None
        self.out_name = ''

        # If the parameter file is not a log.param, the path will be read
        # before reading the parameter file.
        if self.param.find('log.param') == -1:
            self.path.update(path)

        # Read from the parameter file to fill properly the mcmc_parameters
        # dictionary.
        self.fill_mcmc_parameters()

        # Test if the recovered path agrees with the one extracted from
        # the configuration file.
        if self.path != {}:
            if not self.path.has_key('root'):
                self.path.update({'root': path['root']})
            if self.path != path:
                warnings.warn(
                    "Your code location in the log.param file is "
                    "in contradiction with your .conf file. "
                    "I will use the one from log.param.")

        # Determine which cosmological code is in use
        if self.path['cosmo'].find('class') != -1:
            self.cosmological_module_name = 'CLASS'
        else:
            self.cosmological_module_name = None

        # Recover the cosmological code version (and git hash if relevant).
        # To implement a new cosmological code, please add another case to the
        # test below.
        if self.cosmological_module_name == 'CLASS':
            # Official version number
            common_file_path = os.path.join(
                self.path['cosmo'], os.path.join('include', 'common.h'))
            with open(common_file_path, 'r') as common_file:
                for line in common_file:
                    if line.find('_VERSION_') != -1:
                        self.version = line.split()[-1].replace('"', '')
                        break
            print 'with CLASS %s' % self.version
            # Git version number and branch
            try:
                # This nul_file helps to get read of a potential useless error
                # message
                with open(os.devnull, "w") as nul_file:
                    self.git_version = sp.check_output(
                        ["git", "rev-parse", "HEAD"],
                        cwd=self.path['cosmo'],
                        stderr=nul_file).strip()
                    self.git_branch = sp.check_output(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        cwd=self.path['cosmo'],
                        stderr=nul_file).strip()
            except sp.CalledProcessError:
                warnings.warn(
                    "Running CLASS from a non version-controlled repository")
                self.git_version, self.git_branch = '', ''

            # If using an existing log.param, read in and compare this number
            # to the already stored one
            if self.param.find('log.param') != -1:
                try:
                    version, git_version, git_branch = self.read_version(
                        self.param_file)
                    if version != self.version:
                        warnings.warn(
                            "Your version of CLASS: %s" % self.version +
                            " does not match the one used previously" +
                            " in this folder (%s)." % version +
                            " Proceed with caution")
                    else:
                        if self.git_branch != git_branch:
                            warnings.warn(
                                "CLASS set to branch %s" % self.git_branch +
                                ", wrt. the one used in the log.param:" +
                                " %s." % git_branch)
                        if self.git_version != git_version:
                            warnings.warn(
                                "CLASS set to version %s" % self.git_version +
                                ", wrt. the one used in the log.param:" +
                                " %s." % git_version)

                except AttributeError:
                    # This error is raised when the regular expression match
                    # failed - due to comparing to an old log.param that did
                    # not have this feature properly implemented. Ignore this.
                    pass

        else:
            raise io_mp.CosmologicalModuleError(
                "If you want to check for another cosmological module version"
                " please add an elif clause to this part")

        # End of initialisation with the parameter file
        self.param_file.close()

        self.log_flag = False
        """
        Stores the information whether or not the likelihood data files need to
        be written down in the log.param file. Initially at False.

        :rtype: bool
        """

        self.need_cosmo_update = True
        """
        `added in version 1.1.1`. It stores the truth value of whether the
        cosmological block of parameters was changed from one step to another.
        See :meth:`group_parameters_in_blocks`

        :rtype: bool
        """

        # logging the parameter file (only if folder does not exist !)
        ## temporary variable for readability
        log_param = os.path.join(command_line.folder, 'log.param')

        if (os.path.exists(command_line.folder) and
                not os.path.exists(log_param)):
            if command_line.param is not None:
                warnings.warn(
                    "Detecting empty folder, logging the parameter file")
                io_mp.log_parameters(self, command_line)
                self.log_flag = True
        if not os.path.exists(command_line.folder):
            os.makedirs(command_line.folder)
            # Logging of parameters
            io_mp.log_parameters(self, command_line)
            self.log_flag = True

        self.lkl = od()

        # adding the likelihood directory to the path, to import the module
        # then, for each library, calling an instance of the likelihood.
        # Beware, though, if you add new likelihoods, they should go to the
        # folder likelihoods/yourlike/yourlike.py, and contain a yourlike.data,
        # otherwise the following set of commands will not work anymore.

        # For the logging if log_flag is True, each likelihood will log its
        # parameters

        print '\nTesting likelihoods for:\n ->',
        print ', '.join(self.experiments)+'\n'

        # Due to problems in relative import, this line must be there. Until a
        # better solution is found. It adds the root folder of the MontePython
        # used as the first element in the sys.path
        sys.path.insert(0, self.path['root'])

        for elem in self.experiments:

            folder = os.path.abspath(os.path.join(
                path['MontePython'], os.path.join("likelihoods", "%s" % elem)))
            # add the folder of the likelihood to the path of libraries to...
            # ... import easily the likelihood.py program
            exec "from likelihoods.%s import %s" % (
                elem, elem)
            # Initialize the likelihoods. Depending on the values of
            # command_line and log_flag, the routine will call slightly
            # different things. If log_flag is True, the log.param will be
            # appended.
            exec "self.lkl['%s'] = %s('%s/%s.data',\
                self,command_line)" % (
                elem, elem, folder, elem)

        # Storing parameters by blocks of speed
        self.group_parameters_in_blocks()

        # Finally, log the cosmo_arguments used. This comes in the end, because
        # it can be modified inside the likelihoods init functions
        if self.log_flag:
            io_mp.log_cosmo_arguments(self, command_line)
            io_mp.log_default_configuration(self, command_line)

    def fill_mcmc_parameters(self):
        """
        Initializes the ordered dictionary :attr:`mcmc_parameters` from
        the input parameter file.

        It uses :meth:`read_file`, and initializes instances of
        :class:`parameter` to actually fill in :attr:`mcmc_parameters`.

        """

        # Define temporary quantities, only to simplify the input in the
        # parameter file
        self.parameters = od()

        # Read from the parameter file everything
        try:
            self.param_file = open(self.param, 'r')
        except IOError:
            raise io_mp.ConfigurationError(
                "Error in initializing the Data class, the parameter file " +
                "{0} does not point to a proper file".format(self.param))
        # In case the parameter file is a log.param, scan first once the file
        # to extract the path dictionnary.
        if self.param.find('log.param') != -1:
            self.read_file(self.param_file, search_path=True)
        self.read_file(self.param_file)

        for key, value in self.parameters.iteritems():
            self.mcmc_parameters[key] = Parameter(value, key)
        """
        Transform from parameters dictionnary to mcmc_parameters dictionary of
        instances from the class :class:`parameter` (inheriting from dict)
        """

    def read_file(self, param_file, search_path=False):
        """
        Execute all lines concerning the Data class from a parameter file

        All lines starting with `data.` will be replaced by `self.`, so the
        current instance of the class will contain all the information.

        .. note::

            A rstrip() was added at the end, because of an uncomprehensible bug
            on some systems that imagined some unexistant characters at the end
            of the line... Now should work

        .. note::

            A security should be added to protect from obvious attacks.

        .. warning::

            New in version 2.0.0, the file is read twice, because the path
            dictionnary was unfortunately stored in the end of the file.
        """
        for line in param_file:
            if line.find('#') == -1 and line:
                lhs = line.split('=')[0]
                if lhs.find('data.') != -1:
                    if search_path:
                        # Do not execute this line if the flag search_path is
                        # set to True
                        if lhs.find('data.path') == -1:
                            continue
                    exec(line.replace('data.', 'self.').rstrip())
        param_file.seek(0)

    def group_parameters_in_blocks(self):
        """
        Regroup mcmc parameters by blocks of same speed

        This method divides all varying parameters from :attr:`mcmc_parameters`
        into as many categories as there are likelihoods, plus one (the slow
        block of cosmological parameters).

        It creates the attribute :attr:`block_parameters`, to be used in the
        module :mod:`mcmc`.

        .. note::

            It does not compute by any mean the real speed of each parameter,
            instead, every parameter belonging to the same likelihood will
            be considered as fast as its neighbour.

        .. warning::

            It assumes that the nuisance parameters are already written
            sequentially, and grouped together (not necessarilly in the order
            described in :attr:`experiments`). If you mix up the different
            nuisance parameters in the .param file, this routine will not
            method as intended. It also assumes that the cosmological
            parameters are written at the beginning of the file.

        """
        array = []
        # First obvious block is all cosmological parameters
        array.append(len(self.get_mcmc_parameters(['varying', 'cosmo'])))
        # Then, store all nuisance parameters
        nuisance = self.get_mcmc_parameters(['varying', 'nuisance'])

        # Create an array to keep track of the already taken into account
        # nuisance parameters. This will come in handy when using likelihoods
        # that share some nuisance parameters.
        used_nuisance = []
        for likelihood in self.lkl.itervalues():
            count = 0
            for elem in nuisance:
                if elem in likelihood.nuisance:
                    if elem not in used_nuisance:
                        count += 1
                        used_nuisance.append(elem)
            likelihood.varying_nuisance_parameters = count

        # Then circle through them
        index = 0
        while index < len(nuisance):
            elem = nuisance[index]
            flag = False
            # For each one, check if they belong to a likelihood
            for likelihood in self.lkl.itervalues():
                if elem in likelihood.nuisance:
                    # If yes, store the number of nuisance parameters needed
                    # for this likelihood.
                    flag = True
                    array.append(
                        likelihood.varying_nuisance_parameters+array[-1])
                    index += likelihood.varying_nuisance_parameters
                    continue
            if not flag:
                # If the loop reaches this part, it means this nuisance
                # parameter was associated with no likelihood: this should not
                # happen
                raise io_mp.ConfigurationError(
                    "nuisance parameter %s " % elem +
                    "is associated to no likelihood")
        # Store the result
        self.block_parameters = array

        # Setting a default value for the over_sampling array
        if not self.over_sampling:
            self.over_sampling = [1 for _ in range(len(self.block_parameters))]
        # Test that the over_sampling list has the same size as
        # block_parameters.
        else:
            try:
                assert len(self.block_parameters) == len(self.over_sampling)
            except AssertionError:
                raise io_mp.ConfigurationError(
                    "The length of the over_sampling field should be"
                    " equal to the number of blocks (one for cosmological "
                    "parameters, plus one for each likelihood with "
                    "nuisance parameters)")

        # Create a list of indices corresponding of the oversampling strategy
        self.assign_over_sampling_indices()

    def assign_over_sampling_indices(self):
        """
        Create the list of varied parameters given the oversampling
        """
        self.over_sampling_indices = []
        for index in range(len(self.get_mcmc_parameters(['varying']))):
            if index == 0:
                self.over_sampling_indices.append(index)
            else:
                block_index = self.block_parameters.index(
                    [i for i in self.block_parameters if index < i][0])
                for _ in range(self.over_sampling[block_index]):
                    self.over_sampling_indices.append(index)

    def read_version(self, param_file):
        """
        Extract version and subversion from an existing log.param
        """
        # Read the first line (cosmological code version)
        first_line = param_file.readline()
        param_file.seek(0)
        regexp = re.match(
            ".*\(branch: (.*), hash: (.*)\).*",
            first_line)
        version = first_line.split()[1]
        git_branch, git_version = regexp.groups()
        return version, git_version, git_branch

    def get_mcmc_parameters(self, table_of_strings):
        """
        Returns an ordered array of parameter names filtered by
        `table_of_strings`.

        Parameters
        ----------
        table_of_strings : list
            List of strings whose role and status must be matched by a
            parameter. For instance,

            >>> Data.get_mcmc_parameters(['varying'])

            will return a list of all the varying parameters, both
            cosmological and nuisance ones (derived parameters being `fixed`,
            they wont be part of this list). Instead,

            >>> Data.get_mcmc_parameters(['nuisance', 'varying'])

            will only return the nuisance parameters that are being varied.

        """
        table = []
        for key, value in self.mcmc_parameters.iteritems():
            number = 0
            for subvalue in value.itervalues():
                for string in table_of_strings:
                    if subvalue == string:
                        number += 1
            if number == len(table_of_strings):
                table.append(key)
        return table

    def check_for_slow_step(self, new_step):
        """
        Check whether the value of cosmological parameters were
        changed, and if no, skip computation of the cosmology.

        """
        parameter_names = self.get_mcmc_parameters(['varying'])
        cosmo_names = self.get_mcmc_parameters(['cosmo'])

        need_change = 0

        # For all elements in the varying parameters:
        for elem in parameter_names:
            i = parameter_names.index(elem)
            # If it is a cosmological parameter
            if elem in cosmo_names:
                if self.mcmc_parameters[elem]['current'] != new_step[i]:
                    need_change += 1

        # If any cosmological value was changed,
        if need_change > 0:
            self.need_cosmo_update = True
        else:
            self.need_cosmo_update = False

        for likelihood in self.lkl.itervalues():
            # If the cosmology changed, you need to recompute the likelihood
            # anyway
            if self.need_cosmo_update:
                likelihood.need_update = True
                continue
            # Otherwise, check if the nuisance parameters of this likelihood
            # were changed
            need_change = 0
            for elem in parameter_names:
                i = parameter_names.index(elem)
                if elem in likelihood.nuisance:
                    if self.mcmc_parameters[elem]['current'] != new_step[i]:
                        need_change += 1
            if need_change > 0:
                likelihood.need_update = True
            else:
                likelihood.need_update = False

    def update_cosmo_arguments(self):
        """
        Put in :attr:`cosmo_arguments` the current values of
        :attr:`mcmc_parameters`

        This method is called at every step in the Markov chain, to update the
        dictionary. In the Markov chain, the scale is not remembered, so one
        has to apply it before giving it to the cosmological code.

        .. note::

            When you want to define new parameters in the Markov chain that do
            not have a one to one correspondance to a cosmological name, you
            can redefine its behaviour here. You will find in the source
            several such examples.

        """
        # For all elements in any cosmological parameters
        for elem in self.get_mcmc_parameters(['cosmo']):
            # Fill in the dictionnary with the current value of parameters
            self.cosmo_arguments[elem] = \
                self.mcmc_parameters[elem]['current'] *\
                self.mcmc_parameters[elem]['scale']

        # For all elements in the cosmological parameters from the mcmc list,
        # translate any-one that is not directly a CLASS parameter into one.
        # The try: except: syntax ensures that the first call
        for elem in self.get_mcmc_parameters(['cosmo']):
            # infer h from Omega_Lambda and delete Omega_Lambda
            if elem == 'Omega_Lambda':
                omega_b = self.cosmo_arguments['omega_b']
                omega_cdm = self.cosmo_arguments['omega_cdm']
                Omega_Lambda = self.cosmo_arguments['Omega_Lambda']
                self.cosmo_arguments['h'] = math.sqrt(
                    (omega_b+omega_cdm) / (1.-Omega_Lambda))
                del self.cosmo_arguments[elem]
            # infer omega_cdm from Omega_L and delete Omega_L
            if elem == 'Omega_L':
                omega_b = self.cosmo_arguments['omega_b']
                h = self.cosmo_arguments['h']
                Omega_L = self.cosmo_arguments['Omega_L']
                self.cosmo_arguments['omega_cdm'] = (1.-Omega_L)*h*h-omega_b
                del self.cosmo_arguments[elem]
            if elem == 'ln10^{10}A_s':
                self.cosmo_arguments['A_s'] = math.exp(
                    self.cosmo_arguments[elem]) / 1.e10
                del self.cosmo_arguments[elem]
            if elem == 'exp_m_2_tau_As':
                tau_reio = self.cosmo_arguments['tau_reio']
                self.cosmo_arguments['A_s'] = self.cosmo_arguments[elem] * \
                    math.exp(2.*tau_reio)
                del self.cosmo_arguments[elem]
            if elem == 'f_cdi':
                self.cosmo_arguments['n_cdi'] = self.cosmo_arguments['n_s']
            if elem == 'beta':
                self.cosmo_arguments['alpha'] = 2.*self.cosmo_arguments['beta']
            # We only do that on xe_1, for there is at least one of them.
            if elem.find('xe_1') != -1:
                # To pass this option, you must have set a number of
                # cosmological settings reio_parametrization to reio_bins_tanh,
                # binned_reio_z set, and binned_reio_num First, you need to set
                # reio_parametrization to reio_bins_tanh
                if (self.cosmo_arguments['reio_parametrization'] !=
                        'reio_bins_tanh'):
                    raise io_mp.CosmologicalModuleError(
                        "You set binned_reio_xe to some values " +
                        "without setting reio_parametrization to " +
                        "reio_bins_tanh")
                else:
                    try:
                        size = self.cosmo_arguments['binned_reio_num']
                    except (KeyError):
                        raise io_mp.CosmologicalModuleError(
                            "You need to set reio_binnumber to the value" +
                            " corresponding to the one in binned_reio_xe")
                string = ''
                for i in range(1, size+1):
                    string += '%.4g' % self.cosmo_arguments['xe_%d' % i]
                    del self.cosmo_arguments['xe_%d' % i]
                    if i != size:
                        string += ','
                self.cosmo_arguments['binned_reio_xe'] = string
            if elem == 'M_tot':
                self.cosmo_arguments['m_ncdm'] = self.cosmo_arguments['M_tot']/3.
                del self.cosmo_arguments[elem]

    def __cmp__(self, other):
        """
        Redefinition of the 'compare' method for two instances of this class.

        It will decide which basic operations to perform when the code asked if
        two instances are the same (in case you want to launch a new chain in
        an existing folder, with your own parameter file) Comparing
        cosmological code versions (warning only, will not fail the comparison)

        """
        if self.version != other.version:
            warnings.warn(
                "You are running with a different version of your " +
                "cosmological code")

        # Defines unordered version of the dictionaries of parameters
        self.uo_parameters = {}
        other.uo_parameters = {}

        # Check if all the experiments are tested again,
        if len(list(set(other.experiments).symmetric_difference(
                set(self.experiments)))) == 0:
            # Check that they have been called with the same .data file, stored
            # in dictionary when initializing.
            for experiment in self.experiments:
                for elem in self.lkl[experiment].dictionary:
                    if self.lkl[experiment].dictionary[elem] != \
                            other.lkl[experiment].dictionary[elem]:
                        print 'in your parameter file: ',
                        print self.lkl[experiment].dictionary
                        print 'in log.param:           ',
                        print other.lkl[experiment].dictionary
                        return -1
            # Fill in the unordered version of dictionaries
            for key, elem in self.mcmc_parameters.iteritems():
                self.uo_parameters[key] = elem['initial']
            for key, elem in other.mcmc_parameters.iteritems():
                other.uo_parameters[key] = elem['initial']

            # And finally compare them (standard comparison between
            # dictionnaries, will return True if both have the same keys and
            # values associated to them.
            return cmp(self.uo_parameters, other.uo_parameters)
        else:
            return -1

    def __call__(self, ctx):
        """
        Interface layer with CosmoHammer

        Store quantities to a the context, to be accessed by the Cosmo Module
        and each of the likelihoods.

        Parameters
        ----------
        ctx : context
                Contains several dictionaries storing data and cosmological
                information

        """
        # Recover the cosmological parameter value from the context
        parameters = ctx.getParams()

        # Storing them as current points
        for index, elem in enumerate(self.get_mcmc_parameters(["varying"])):
            self.mcmc_parameters[elem]['current'] = parameters[index]

        # Propagating this to the cosmo_arguments dictionary
        self.update_cosmo_arguments()

        # Store itself into the context
        ctx.add('data', self)


class Parameter(dict):
    """
    Store all important fields, and define a few convenience methods

    """
    def __init__(self, array, key):
        """
        This class replaces the old function defined in the Data class, called
        `from_input_to_mcmc_parameters`. The traduction is now done inside the
        Parameter class, which interprets the array given as an input inside
        the parameter file, and returns a dictionary having all relevant fields
        initialized.

        .. warning::

            This used to be an ordered dictionary, for no evident reason. It is
            now reverted back to an ordinary dictionary. If this broke
            anything, it will be reverted back

        At the end of this initialization, every field but one is filled for
        the specified parameter, be it fixed or varying. The missing field is
        the 'last_accepted' one, that will be filled in the module :mod:`mcmc`.

        .. note::

            The syntax of the parameter files is defined here - if one
            wants to change it, one should report the changes in there.

        The other fields are

        Attributes
        ----------
        initial : array
            Initial array of input values defined in the parameter file.
            Contains (in this order) `mean`, `minimum`, `maximum`, `1-sigma`.
            If the min/max values (**TO CHECK** proposal density boundaries)
            are unimportant/unconstrained, use `None` or `-1` (without a period
            !)
        scale : float
            5th entry of the initial array in the parameter file, defines the
            factor with which to multiply the values defined in `initial` to
            give the real value.
        role : str
            6th entry of the initial array, can be `cosmo`, `nuisance` or
            `derived`. A `derived` parameter will not be considered as varying,
            but will be instead recovered from the cosmological code for each
            point in the parameter space.
        prior : :class:`Prior <prior.Prior>`
            defined through the optional 7th entry of the initial array, can be
            ommited or set to `flat` (same), or set to `gaussian`. An instance
            of the :class:`prior` defined in :mod:`prior` will be initialized
            and set to this value.
        tex_name : str
            A tentative tex version of the name, provided by the function
            :func:`io_mp.get_tex_name`.
        status : str
            Depending on the `1-sigma` value in the initial array, it will be
            set to `fixed` or `varying` (resp. zero and non-zero)
        current : float
            Stores the value at the current point in parameter space (`not
            allowed initially`)

        Parameters
        ----------
        value : list
            Array read from the parameter file
        key : str
            Name of the parameter

        """
        # calling the parent method initialization
        dict.__init__(self)

        self['initial'] = array[0:4]
        self['scale'] = array[4]
        self['role'] = array[-1]
        self['tex_name'] = io_mp.get_tex_name(key)
        if array[3] == 0:
            self['status'] = 'fixed'
            self['current'] = array[0]
        else:
            self['status'] = 'varying'
        self['prior'] = prior.Prior(array)
