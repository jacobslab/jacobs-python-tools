"""
Subsequent Memory Effect Analysis after correct for 1/f slope of the power spectrum.
For every electrode and frequency, compare correctly and incorrectly recalled items using a t-test.
"""
import os
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
import numexpr as ne
import statsmodels.api as sm
from mpl_toolkits.axes_grid1 import make_axes_locatable
from scipy.stats import sem, ttest_ind
from joblib import Parallel, delayed
from fooof import FOOOF

from JacobsPythonTools.SubjectLevel.subject_analysis import SubjectAnalysisBase
from JacobsPythonTools.SubjectLevel.subject_ram_power_data import SubjectRamPowerData


class SubjectFitSpectraAnalysis(SubjectAnalysisBase, SubjectRamPowerData):
    """

    The user must define the .recall_filter_func attribute of this class. This should be a function that, given a set
    of events, returns a boolean array of recalled (True) and not recalled (False) items.
    """

    def __init__(self, task=None, subject=None, montage=0):
        super(SubjectFitSpectraAnalysis, self).__init__(task=task, subject=subject, montage=montage)

        # string to use when saving results files
        self.res_str = 'fit_spectra.p'

        # The SME analysis is a contrast between two conditions (recalled and not recalled items). Set
        # recall_filter_func to be a function that takes in events and returns bool of recalled items
        self.recall_filter_func = None

        # If True, use the FOOOF algorithm for fitting the power spectra. If false, use a robust regression
        # See https://github.com/voytekresearch/fooof
        self.use_fooof = False

        # NOTE 1: fooof is too slow to realistically compute for every event, electrode, (and timebin, if present).
        # the default when using fooof is to only fit the mean power spectra for each condition. Change the parameter
        # if you feel like waiting a LONG time only.

        # NOTE 2: if this is False, then we can't run t-tests at a within electrode level. We can still look at the
        # difference between the means of the conditions
        self.fooof_fit_each_event = False

    def _generate_res_save_path(self):
        self.res_save_dir = os.path.join(os.path.split(self.save_dir)[0], self.__class__.__name__+'_res')

    def analysis(self):
        """
        Run the 1/f fitting or fooof and compute SME.
        """
        if self.subject_data is None:
            print('%s: compute or load data first with .load_data()!' % self.subject)

        # Get recalled or not labels
        if self.recall_filter_func is None:
            print('%s SME: please provide a .recall_filter_func function.' % self.subject)
        recalled = self.recall_filter_func(self.subject_data)

        # normalized power spectra
        # p_spects = self.normalize_power_spectrum()
        p_spects = self.subject_data.data

        # and fit each channel and event. This parallelizes over whatever the last dimension
        # if number of dimensions is 4, then we have time bins. We want to parallelize over channels or time, depending
        # on which dimension is bigger. This will also help with memory usage of the stats down below
        is_swapped = False
        if (p_spects.ndim == 4) and (p_spects.shape[3] < p_spects.shape[2]):
            p_spects = p_spects.swapaxes(2, 3)
            is_swapped = True

        # if we are using robust regression, add a constant column to the indep var
        if not self.use_fooof:
            f = robust_reg
            x = sm.tools.tools.add_constant(np.log10(self.freqs))

        # if fooof, our indep var is just the freqs with no constant.
        # also, fooof wants the y vals not in log space, so undo if we have already logged the power values
        else:
            f = run_foof
            x = self.freqs
            if self.log_power:
                p_spects = self.subject_data.data
                p_spects = ne.evaluate("10**p_spects")

            # only give fooof the mean of each condition.
            if not self.fooof_fit_each_event:
                p_spects = np.stack([p_spects[recalled].mean(axis=0), p_spects[~recalled].mean(axis=0)], 0)

        # run the fitting procedure
        res = Parallel(n_jobs=12, verbose=5)(delayed(f)(x, y.T) for y in p_spects.T)

        if (not self.use_fooof) or (self.use_fooof and self.fooof_fit_each_event):

            # for every frequency, electrode, timebin, subtract mean recalled from mean non-recalled
            delta_resid = [np.nanmean(x[2][recalled], axis=0) - np.nanmean(x[2][~recalled], axis=0) for x in res]
            delta_resid = np.stack(delta_resid, -1)
            delta_slopes = [np.nanmean(x[0][recalled], axis=0) - np.nanmean(x[0][~recalled], axis=0) for x in res]
            delta_slopes = np.stack(delta_slopes, -1)
            delta_offsets = [np.nanmean(x[1][recalled], axis=0) - np.nanmean(x[1][~recalled], axis=0) for x in res]
            delta_offsets = np.stack(delta_offsets, -1)

            # run ttest at each frequency and electrode comparing remembered and not remembered events on resids
            ttest_resid = [ttest_ind(x[2][recalled], x[2][~recalled], axis=0) for x in res]
            ts_resid = np.stack([x.statistic for x in ttest_resid], -1)
            ps_resid = np.stack([x.pvalue for x in ttest_resid], -1)

            # also compare slopes
            ttest_slopes = [ttest_ind(x[0][recalled], x[0][~recalled], axis=0) for x in res]
            ts_slopes = np.stack([x.statistic for x in ttest_slopes], -1)
            ps_slopes = np.stack([x.pvalue for x in ttest_slopes], -1)

            # and offsets
            ttest_slopes = [ttest_ind(x[1][recalled], x[1][~recalled], axis=0) for x in res]
            ts_offsets = np.stack([x.statistic for x in ttest_slopes], -1)
            ps_offsets = np.stack([x.pvalue for x in ttest_slopes], -1)

            self.res['ts_resid'] = ts_resid if not is_swapped else np.swapaxes(ts_resid, 1, 2)
            self.res['ps_resid'] = ps_resid if not is_swapped else np.swapaxes(ps_resid, 1, 2)
            self.res['ts_slopes'] = ts_slopes if not is_swapped else np.swapaxes(ts_slopes, 0, 1)
            self.res['ps_slopes'] = ps_slopes if not is_swapped else np.swapaxes(ps_slopes, 0, 1)
            self.res['ts_offsets'] = ts_offsets if not is_swapped else np.swapaxes(ts_offsets, 0, 1)
            self.res['ps_offsets'] = ps_offsets if not is_swapped else np.swapaxes(ps_offsets, 0, 1)

        elif self.use_fooof and not self.fooof_fit_each_event:
            delta_resid = np.stack([x[2][0] - x[2][1] for x in res], -1)
            delta_slopes = np.stack([x[0][0] - x[0][1] for x in res], -1)
            delta_offsets = np.stack([x[1][0] - x[1][1] for x in res], -1)

        # store results. Swap the axes back if we swapped them
        self.res['delta_resid'] = delta_resid if not is_swapped else np.swapaxes(delta_resid, 1, 2)
        self.res['delta_slopes'] = delta_slopes if not is_swapped else np.swapaxes(delta_slopes, 1, 2)
        self.res['delta_offsets'] = delta_offsets if not is_swapped else np.swapaxes(delta_offsets, 1, 2)
        self.res['p_recall'] = np.mean(recalled)
        self.res['recalled'] = recalled

    def plot_spectra_average(self, elec_label='', region_column='', loc_tag_column=''):
        """
        Create a two panel figure with shared x-axis. Top panel is log(power) as a function of frequency, seperately
        plotted for recalled (red) and not-recalled (blue) items. Bottom panel is t-stat at each frequency comparing the
        recalled and not recalled distributions, with shaded areas indicating p<.05.

        elec_label: electrode label that you wish to plot.
        region_column: column of the elec_info dataframe that will be used to label the plot
        loc_tag_column: another of the elec_info dataframe that will be used to label the plot
        """
        if self.subject_data is None:
            print('%s: data must be loaded before computing SME by region. Use .load_data().' % self.subject)
            return

        if not self.res:
            print('%s: must run .analysis() before computing SME by region' % self.subject)
            return

        # get the index into the data for this electrode
        elec_ind = self.subject_data.channel == elec_label
        if ~np.any(elec_ind):
            print('%s: must enter a valid electrode label, as found in self.subject_data.channel' % self.subject)
            return

        # normalize spectra
        recalled = self.res['recalled']
        p_spect = self.normalize_power_spectrum()

        # create axis
        with plt.style.context('fivethirtyeight'):
            with mpl.rc_context({'ytick.labelsize': 16,
                                 'xtick.labelsize': 16}):
                ax1 = plt.subplot2grid((3, 1), (0, 0), rowspan=2)
                ax2 = plt.subplot2grid((3, 1), (2, 0), rowspan=1)

                # will plot in log space
                x = np.log10(self.subject_data.frequency)

                ###############
                ## Top panel ##
                ###############
                # recalled mean and err
                rec_mean = np.mean(p_spect[recalled, :, elec_ind], axis=0)
                rec_sem = sem(p_spect[recalled, :, elec_ind], axis=0)
                ax1.plot(x, rec_mean, c='#8c564b', label='Good Memory', linewidth=2)
                ax1.fill_between(x, rec_mean + rec_sem, rec_mean - rec_sem, color='#8c564b', alpha=.5)

                # not recalled mean and err
                nrec_mean = np.mean(p_spect[~recalled, :, elec_ind], axis=0)
                nrec_sem = sem(p_spect[~recalled, :, elec_ind], axis=0)
                ax1.plot(x, nrec_mean, color='#1f77b4', label='Bad Memory', linewidth=2)
                ax1.fill_between(x, nrec_mean + nrec_sem, nrec_mean - nrec_sem, color='#1f77b4', alpha=.5)

                # y labels and y ticks
                ax1.set_ylabel('Normalized log(power)')
                ax1.yaxis.label.set_fontsize(24)
                ax1.yaxis.set_ticks([-2, -1, 0, 1, 2])
                ax1.set_ylim([-2, 2])

                # make legend
                l = ax1.legend()
                frame = l.get_frame()
                frame.set_facecolor('w')
                for legobj in l.legendHandles:
                    legobj.set_linewidth(5)

                ##################
                ## Bottom panel ##
                ##################
                y = np.squeeze(self.res['ts'][:, elec_ind])
                p = np.squeeze(self.res['ps'][:, elec_ind])
                ax2.plot(x, y, '-k', linewidth=4)
                ax2.set_ylim([-np.max(np.abs(ax2.get_ylim())), np.max(np.abs(ax2.get_ylim()))])
                ax2.plot(x, np.zeros(x.shape), c=[.5, .5, .5], zorder=1)
                ax2.fill_between(x, [0] * len(x), y, where=(p < .05) & (y > 0), facecolor='#8c564b', edgecolor='#8c564b')
                ax2.fill_between(x, [0] * len(x), y, where=(p < .05) & (y < 0), facecolor='#1f77b4', edgecolor='#1f77b4')
                ax2.set_ylabel('t-stat')
                plt.xlabel('Frequency', fontsize=24)
                ax2.yaxis.label.set_fontsize(24)
                ax2.yaxis.set_ticks([-2, 0, 2])

                # put powers of two on the x-axis for both panels
                new_x = self.compute_pow_two_series()
                ax2.xaxis.set_ticks(np.log10(new_x))
                ax2.xaxis.set_ticklabels(new_x, rotation=0)
                ax1.xaxis.set_ticks(np.log10(new_x))
                ax1.xaxis.set_ticklabels('')

                # get some localization info for the title
                elec_info_chan = self.elec_info[self.elec_info.label == elec_label]
                title_str = ''
                for col in [region_column, loc_tag_column]:
                    if col:
                        title_str += ' ' + str(elec_info_chan[col].values) + ' '

                _ = ax1.set_title('%s - %s' % (self.subject, elec_label) + title_str)
                plt.gcf().set_size_inches(12, 9)

        return plt.gcf()

    def plot_elec_heat_map(self, sortby_column1='', sortby_column2=''):
        """
        Frequency by electrode SME visualization.

        Plots a channel x frequency visualization of the subject's data
        sortby_column1: if given, will sort the data by this column and then plot
        sortby_column2: secondary column for sorting
        """

        # group the electrodes by region, if we have the info
        do_region = True
        if sortby_column1 and sortby_column2:
            regions = self.elec_info[sortby_column1].fillna(self.elec_info[sortby_column2]).fillna(value='')
            elec_order = np.argsort(regions)
            groups = np.unique(regions)
        elif sortby_column1:
            regions = self.elec_info[sortby_column1].fillna(value='')
            elec_order = np.argsort(regions)
            groups = np.unique(regions)
        else:
            elec_order = np.arange(self.elec_info.shape[0])
            do_region = False

        # make dataframe of results for easier plotting
        df = pd.DataFrame(self.res['ts'], index=self.freqs,
                          columns=self.subject_data.channel)
        df = df.T.iloc[elec_order].T

        # make figure. Add axes for colorbar
        with mpl.rc_context({'ytick.labelsize': 14,
                             'xtick.labelsize': 14,
                             'axes.labelsize': 20}):
            fig, ax = plt.subplots()
            divider = make_axes_locatable(ax)
            cax = divider.append_axes('right', size='3%', pad=0.1)

            # plot heatmap
            plt.gcf().set_size_inches(18, 12)
            clim = np.max(np.abs(self.res['ts']))
            sns.heatmap(df, cmap='RdBu_r', linewidths=.5,
                        yticklabels=df.index.values.round(2), ax=ax,
                        cbar_ax=cax, vmin=-clim, vmax=clim, cbar_kws={'label': 't-stat'})
            ax.set_xlabel('Channel', fontsize=24)
            ax.set_ylabel('Frequency (Hz)', fontsize=24)
            ax.invert_yaxis()

            # if plotting region info
            if do_region:
                ax2 = divider.append_axes('top', size='3%', pad=0)
                for i, this_group in enumerate(groups):
                    x = np.where(regions[elec_order] == this_group)[0]
                    ax2.plot([x[0] + .5, x[-1] + .5], [0, 0], '-', color=[.7, .7, .7])
                    if len(x) > 1:
                        if ' ' in this_group:
                            this_group = this_group.split()[0]+' '+''.join([x[0].upper() for x in this_group.split()[1:]])
                        else:
                            this_group = this_group[:12] + '.'
                        plt.text(np.mean([x[0] + .5, x[-1] + .5]), 0.05, this_group,
                                 fontsize=14,
                                 horizontalalignment='center',
                                 verticalalignment='bottom', rotation=90)
                ax2.set_xlim(ax.get_xlim())
                ax2.set_yticks([])
                ax2.set_xticks([])
                ax2.axis('off')

    def compute_pow_two_series(self):
        """
        This convoluted line computes a series powers of two up to and including one power higher than the
        frequencies used. Will use this as our axis ticks and labels so we can have nice round values.
        """
        return np.power(2, range(int(np.log2(2 ** (int(self.freqs[-1]) - 1).bit_length())) + 1))


def run_foof(x, y):
    """
    Fits the FOOOF (fitting oscillations & one over f) model.

    Returns slopes (num obs), offsets (num obs), and the peak fit power spectra (num obs x num features)
    """
    res_shape = (y.shape[0], y.shape[-1]) if y.ndim == 3 else y.shape[0]
    slopes = np.full(res_shape, np.nan, dtype='float32')
    offsets = np.full(res_shape, np.nan, dtype='float32')
    resids = np.full(y.shape, np.nan, dtype='float32')

    # initialize foof
    fm = FOOOF(peak_width_limits=[1.0, 8.0], peak_threshold=0.5)

    for i, this_event in enumerate(y):
        if this_event.ndim == 2:
            freq_dim = np.array([n == len(x) for n in this_event.shape])
            freq_dim_num = np.where(freq_dim)[0][0]
            if freq_dim_num == 0:
                this_event = this_event.T
            for j, this_event_sub in enumerate(this_event):
                fm.add_data(x, y)
                fm.fit()
                offsets[i, j] = fm.background_params_[0]
                slopes[i, j] = fm.background_params_[1]
                resids[i, :, j] = fm._peak_fit
        else:
            fm.add_data(x, this_event)
            fm.fit()
            offsets[i] = fm.background_params_[0]
            slopes[i] = fm.background_params_[1]
            resids[i] = fm._peak_fit

    return slopes, offsets, resids


def robust_reg(x, y):
    """
    Fits a robust regression, looping over each entry in y.

    Returns slopes (num obs), offsets (num obs), and residuals (num obs x num features)
    """
    res_shape = (y.shape[0], y.shape[-1]) if y.ndim == 3 else y.shape[0]
    slopes = np.full(res_shape, np.nan, dtype='float32')
    offsets = np.full(res_shape, np.nan, dtype='float32')
    resids = np.full(y.shape, np.nan, dtype='float32')

    for i, this_event in enumerate(y):
        if this_event.ndim == 2:
            freq_dim = np.array([n == len(x) for n in this_event.shape])
            freq_dim_num = np.where(freq_dim)[0][0]
            if freq_dim_num == 0:
                this_event = this_event.T
            for j, this_event_sub in enumerate(this_event):
                rlm = sm.robust.robust_linear_model.RLM(this_event_sub, x,
                                                       M=sm.robust.norms.LeastSquares())
                rlm_results = rlm.fit()
                offsets[i,j] = rlm_results.params[0]
                slopes[i,j] = rlm_results.params[1]
                resids[i, :, j] = rlm_results.resid
        else:
            rlm = sm.robust.robust_linear_model.RLM(this_event, x, M=sm.robust.norms.LeastSquares())
            rlm_results = rlm.fit()
            offsets[i] = rlm_results.params[0]
            slopes[i] = rlm_results.params[1]
            resids[i] = rlm_results.resid
    return slopes, offsets, resids