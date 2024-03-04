"""
Objects to represent continguous sections of samples in a Neuralynx continuously sampled .Ncs
file. These sections are considered contiguous in that they have a start time, a sampling rate,
and a length.

Defining these sections is complicated due to the physical structure of .Ncs files which contain
both a header and a set of fixed length records, CscRecords.

Each CscRecord has a start time in microseconds, a channel id (chan_id), a stated sampling
frequency (sampFreq) which is rounded to the whole sample/s, and a number of valid samples
(nb_valid), which may be less than the physical maximum of 512. In principle each of these
parameters may vary in each record in the file; however, there are no known examples of .Ncs
files where the chan_id or sampFreq varied from record to record.

The header normally, though not always, contains a stated sampling frequency, which may be
rounded to a whole number of samples per second or not.

Finally, the actual sampling frequency used within a section may be slightly different than any of
the two frequencies above (that in the records or that in the header). This often arises due to
clock drift over the course of a longer recording combined with the fact that only whole
microseconds are recorded in the CscRecords. This can easily be 0.01% amounting to 0.2 seconds over
the course of a half hour recording.

These files may often contain apparent gaps of time between the sequential CscRecords in a file.
A gap is where the predicted time at the start of the next record, based on taking the start time
of the record and then adding 1/sampling rate x nbValid, does not agree exactly with the start time
given in the next record.

These gaps vary from those of less than one microsecond, likely due to rounding of the start time,
to gaps of many minutes, which may be introduced by the human operator stopping and starting
recording during a recording session.

The challenge addressed by the NcsSectionsFactory of this module is to separate those intended
gaps from those introduced by a quirk of the hardware, recording software, or file format.
"""
import math
import numpy as np


class NcsSections:
    """
    Contains information regarding the contiguous sections of records in an Ncs file.
    Methods of NcsSectionsFactory perform parsing of this information from an Ncs file and
    produce these where the sections are discontiguous in time and in temporal order.

    TODO: This class will likely need __ne__ to be useful in
    more sophisticated segment construction algorithms.

    """

    def __init__(self):
        self.sects = []
        self.sampFreqUsed = 0  # actual sampling frequency of samples
        self.microsPerSampUsed = 0  # microseconds per sample

    def __eq__(self, other):
        samp_eq = self.sampFreqUsed == other.sampFreqUsed
        micros_eq = self.microsPerSampUsed == other.microsPerSampUsed
        sects_eq = self.sects == other.sects
        return samp_eq and micros_eq and sects_eq

    def __hash__(self):
        return (f"{self.sampFreqUsed};{self.microsPerSampUsed};" f"{[s.__hash__() for s in self.sects]}").__hash__()

    def is_equivalent(self, other, rel_tol=0, abs_tol=0):
        if len(self.sects) != len(other.sects):
            return False
        else:
            # do not check for gaps if only a single section is present
            for sec_id in range(len(self.sects) - 1):
                if not self.sects[sec_id].is_equivalent(other.sects[sec_id], rel_tol=rel_tol, abs_tol=abs_tol):
                    return False
            return True


class NcsSection:
    """
    Information regarding a single contiguous section or group of records in an Ncs file.

    Model is that times are closed on the left and open on the right. Record
    numbers are closed on both left and right, that is, inclusive of the last record.

    endTime should never be set less than startTime for comparison functions to work
    properly, though this is not enforced.
    """

    _RECORD_SIZE = 512  # nb sample per signal record

    def __init__(self, startRec, startTime, endRec, endTime, n_samples):
        self.startRec = startRec # index of starting record
        self.startTime = startTime # starttime of first record
        self.endRec = endRec # index of last record (inclusive)
        self.endTime = endTime # end time of last record, that is, the end time of the last
                          # sampling period contained in the last record of the section
        self.n_samples = n_samples # number of samples in record which are valid

    def __eq__(self, other):
        return (
            self.startRec == other.startRec
            and self.startTime == other.startTime
            and self.endRec == other.endRec
            and self.endTime == other.endTime
            and self.n_samples == other.n_samples
        )

    def __hash__(self):
        s = f"{self.startRec};{self.startTime};{self.endRec};{self.endTime};{self.n_samples}"
        return s.__hash__()

    def is_equivalent(self, other, rel_tol=0, abs_tol=0):
        eq_start = math.isclose(self.startTime, other.startTime, rel_tol=rel_tol, abs_tol=abs_tol)
        eq_end = math.isclose(self.endTime, other.endTime, rel_tol=rel_tol, abs_tol=abs_tol)
        return eq_start & eq_end

    def before_time(self, rhb):
        """
        Determine if this section is completely before another section in time.
        """
        return self.endTime < rhb.startTime

    def overlaps_time(self, rhb):
        """
        Determine if this section overlaps another in time.
        """
        return self.startTime <= rhb.endTime and self.endTime >= rhb.startTime

    def after_time(self, rhb):
        """
        Determine if this section is completely after another section in time.
        """
        return self.startTime >= rhb.endTime


class NcsSectionsFactory:
    """
    Class for factory methods which perform parsing of contiguous sections of records
    in Ncs files.

    Model for times is that times are rounded to nearest microsecond. Times
    from start of a sample until just before the next sample are included,
    that is, closed lower bound and open upper bound on intervals. A
    channel with no samples is empty and contains no time intervals.

    Moved here since algorithm covering all 3 header styles and types used is
    more complicated.
    """

    _maxGapSampFrac = 0.2  # maximum fraction of a sampling interval between predicted
    # and actual record timestamps still considered within one section

    @staticmethod
    def get_freq_for_micros_per_samp(micros):
        """
        Compute fractional sampling frequency, given microseconds per sample.
        """
        return 1e6 / micros

    @staticmethod
    def get_micros_per_samp_for_freq(sampFr):
        """
        Calculate fractional microseconds per sample, given the sampling frequency (Hz).
        """
        return 1e6 / sampFr

    @staticmethod
    def calc_sample_time(sampFr, startTime, posn):
        """
        Calculate time rounded to microseconds for sample given frequency,
        start time, and sample position.
        """
        return round(startTime + NcsSectionsFactory.get_micros_per_samp_for_freq(sampFr) * posn)

    @staticmethod
    def _parseGivenActualFrequency(ncsMemMap, ncsSects, chanNum, reqFreq, blkOnePredTime):
        """
        Parse sections in memory mapped file when microsPerSampUsed and sampFreqUsed are known,
        filling in an NcsSections object.

        Parameters
        ----------
        ncsMemMap:
            memmap of Ncs file
        ncsSections:
            NcsSections with actual sampFreqUsed correct, first NcsSection with proper startSect
            and startTime already added.
        chanNum:
            channel number that should be present in all records
        reqFreq:
            rounded frequency that all records should contain
        blkOnePredTime:
            predicted starting time of second record in block

        Returns
        -------
        NcsSections object with block locations marked
        """

        # New code numpy vector based (speedup X50)
        delta = (ncsMemMap["timestamp"][1:] - ncsMemMap["timestamp"][:-1]).astype(np.int64)
        delta_prediction = ((ncsMemMap["nb_valid"][:-1] / ncsSects.sampFreqUsed) * 1e6).astype(np.int64)
        gap_inds = np.flatnonzero((delta - delta_prediction) != 0)
        gap_inds += 1
        sections_limits = [ 0 ] + gap_inds.tolist() + [len(ncsMemMap)]

        for i in range(len(gap_inds) + 1):
            start = sections_limits[i]
            stop = sections_limits[i + 1]
            ncsSects.sects.append(
                NcsSection(
                    startRec=start,
                    startTime=ncsMemMap["timestamp"][start],
                    endRec=stop-1,
                    endTime=ncsMemMap["timestamp"][stop-1] + np.uint64(ncsMemMap["nb_valid"][stop-1] / ncsSects.sampFreqUsed * 1e6),
                    n_samples=np.sum(ncsMemMap["nb_valid"][start:stop])
                )
            )

        return ncsSects

    @staticmethod
    def _buildGivenActualFrequency(ncsMemMap, actualSampFreq, reqFreq):
        """
        Build NcsSections object for file given actual sampling frequency.

        Requires that frequency in each record agrees with requested frequency. This is
        normally obtained by rounding the header frequency; however, this value may be different
        from the rounded actual frequency used in the recording, since the underlying
        requirement in older Ncs files was that the number of microseconds per sample in the
        records is the inverse of the sampling frequency stated in the header truncated to
        whole microseconds.

        Parameters
        ----------
        ncsMemMap:
            memmap of Ncs file
        actualSampFreq:
            actual sampling frequency used
        reqFreq:
            frequency to require in records

        Returns
        -------
            NcsSections object
        """
        # check frequency in first record
        if ncsMemMap["sample_rate"][0] != reqFreq:
            raise IOError("Sampling frequency in first record doesn't agree with header.")
        chanNum = ncsMemMap["channel_id"][0]

        ncsSects = NcsSections()
        ncsSects.sampFreqUsed = actualSampFreq
        ncsSects.microsPerSampUsed = NcsSectionsFactory.get_micros_per_samp_for_freq(actualSampFreq)

        # check if file is one block of records, which is often the case, and avoid full parse
        lastBlkI = ncsMemMap.shape[0] - 1
        ts0 = ncsMemMap["timestamp"][0]
        nb0 = ncsMemMap["nb_valid"][0]
        predLastBlockStartTime = NcsSectionsFactory.calc_sample_time(
            actualSampFreq, ts0, NcsSection._RECORD_SIZE * lastBlkI
        )
        lts = ncsMemMap["timestamp"][lastBlkI]
        lnb = ncsMemMap["nb_valid"][lastBlkI]
        if (
            ncsMemMap["channel_id"][lastBlkI] == chanNum
            and ncsMemMap["sample_rate"][lastBlkI] == reqFreq
            and lts == predLastBlockStartTime
        ):
            lastBlkEndTime = NcsSectionsFactory.calc_sample_time(actualSampFreq, lts, lnb)
            n_samples = NcsSection._RECORD_SIZE * lastBlkI
            curBlock = NcsSection(0, ts0, lastBlkI, lastBlkEndTime, n_samples)

            ncsSects.sects.append(curBlock)
            return ncsSects

        
        else:
            # otherwise need to scan looking for data gaps
            blkOnePredTime = NcsSectionsFactory.calc_sample_time(actualSampFreq, ts0, nb0)
            # curBlock = NcsSection(0, ts0, -1, -1, -1)
            # ncsSects.sects.append(curBlock)
            ncsSects = NcsSectionsFactory._parseGivenActualFrequency(ncsMemMap, ncsSects, chanNum, reqFreq, blkOnePredTime)
            return ncsSects

    @staticmethod
    def _parseForMaxGap(ncsMemMap, ncsSects, maxGapLen):
        """
        Parse blocks of records from file, allowing a maximum gap in timestamps between records
        in sections. Estimates frequency being used based on timestamps.

        Parameters
        ----------
        ncsMemMap:
            memmap of Ncs file
        ncsSects:
            NcsSections object with sampFreqUsed set to nominal frequency to use in computing time
            for samples (Hz)
        maxGapLen:
            maximum difference within a block between predicted time of start of record and
            recorded time

        Returns
        -------
            NcsSections object with sampFreqUsed and microsPerSamp set based on estimate from
            largest block
        """

        chanNum = ncsMemMap["channel_id"][0]
        recFreq = ncsMemMap["sample_rate"][0]

        # check for consistent channel_ids and sampling rates
        ncsMemMap["channel_id"]
        if not (ncsMemMap["channel_id"] == chanNum).all():
            raise IOError("Channel number changed in records within file")

        if not all(ncsMemMap["sample_rate"] == recFreq):
            raise IOError("Sampling frequency changed in records within file")

        # find most frequent number of samples
        exp_nb_valid = np.argmax(np.bincount(ncsMemMap["nb_valid"]))
        # detect records with incomplete number of samples
        gap_rec_ids = list(np.where(ncsMemMap["nb_valid"] != exp_nb_valid)[0])

        rec_duration = 1e6 / ncsSects.sampFreqUsed * ncsMemMap["nb_valid"]
        pred_times = np.rint(ncsMemMap["timestamp"] + rec_duration).astype(np.int64)
        max_pred_times = pred_times + maxGapLen
        # data records that start later than the predicted time (including the
        # maximal accepted gap length) are considered delayed and a gap is
        # registered.
        delayed_recs = list(np.where(max_pred_times[:-1] < ncsMemMap["timestamp"][1:])[0])
        gap_rec_ids.extend(delayed_recs)

        # cleaning extracted gap ids
        # last record can not be the beginning of a gap
        last_rec_id = len(ncsMemMap["timestamp"]) - 1
        if last_rec_id in gap_rec_ids:
            gap_rec_ids.remove(last_rec_id)

        # gap ids can only be listed once
        gap_rec_ids = sorted(set(gap_rec_ids))

        # create recording segments from identified gaps
        ncsSects.sects.append(NcsSection(0, ncsMemMap["timestamp"][0], -1, -1, -1))
        for gap_rec_id in gap_rec_ids:
            curr_sec = ncsSects.sects[-1]
            curr_sec.endRec = gap_rec_id
            curr_sec.endTime = pred_times[gap_rec_id]
            n_samples = np.sum(ncsMemMap["nb_valid"][curr_sec.startRec : gap_rec_id + 1])
            curr_sec.n_samples = n_samples

            next_sec = NcsSection(gap_rec_id + 1, ncsMemMap["timestamp"][gap_rec_id + 1], -1, -1, -1)
            ncsSects.sects.append(next_sec)

        curr_sec = ncsSects.sects[-1]
        curr_sec.endRec = len(ncsMemMap["timestamp"]) - 1
        curr_sec.endTime = pred_times[-1]
        n_samples = np.sum(ncsMemMap["nb_valid"][curr_sec.startRec :])
        curr_sec.n_samples = n_samples

        # calculate the estimated frequency of the block with the most samples
        max_blk_idx = np.argmax([bl.endRec - bl.startRec for bl in ncsSects.sects])
        max_blk = ncsSects.sects[max_blk_idx]

        maxBlkFreqEstimate = (
            (max_blk.n_samples - ncsMemMap["nb_valid"][max_blk.endRec])
            * 1e6
            / (ncsMemMap["timestamp"][max_blk.endRec] - max_blk.startTime)
        )

        ncsSects.sampFreqUsed = maxBlkFreqEstimate
        ncsSects.microsPerSampUsed = NcsSectionsFactory.get_micros_per_samp_for_freq(maxBlkFreqEstimate)
        # free memory that is unnecessarily occupied by the memmap
        # (see https://github.com/numpy/numpy/issues/19340)
        del ncsMemMap
        return ncsSects

    @staticmethod
    def _buildForMaxGap(ncsMemMap, nomFreq):
        """
        Determine sections of records in memory mapped Ncs file given a nominal frequency of
        the file, using the default values of frequency tolerance and maximum gap between blocks.

        Parameters
        ----------
        ncsMemMap:
            memmap of Ncs file
        nomFreq:
            nominal sampling frequency used, normally from header of file

        Returns
        -------
            NcsSections object
        """
        nb = NcsSections()

        numRecs = ncsMemMap.shape[0]
        if numRecs < 1:
            return nb

        chanNum = ncsMemMap["channel_id"][0]
        ts0 = ncsMemMap["timestamp"][0]

        lastBlkI = numRecs - 1
        lts = ncsMemMap["timestamp"][lastBlkI]
        lcid = ncsMemMap["channel_id"][lastBlkI]
        lnb = ncsMemMap["nb_valid"][lastBlkI]
        lsr = ncsMemMap["sample_rate"][lastBlkI]

        # check if file is one block of records, with exact timestamp match, which may be the case
        numSampsForPred = NcsSection._RECORD_SIZE * lastBlkI
        predLastBlockStartTime = NcsSectionsFactory.calc_sample_time(nomFreq, ts0, numSampsForPred)
        freqInFile = math.floor(nomFreq)
        if lts - predLastBlockStartTime == 0 and lcid == chanNum and lsr == freqInFile:
            endTime = NcsSectionsFactory.calc_sample_time(nomFreq, lts, lnb)
            curBlock = NcsSection(0, ts0, lastBlkI, endTime, numSampsForPred)
            nb.sects.append(curBlock)
            nb.sampFreqUsed = (numSampsForPred + lnb) / (endTime - ts0) * 1e6
            nb.microsPerSampUsed = NcsSectionsFactory.get_micros_per_samp_for_freq(nb.sampFreqUsed)

        # otherwise parse records to determine blocks using default maximum gap length
        else:
            nb.sampFreqUsed = nomFreq
            nb.microsPerSampUsed = NcsSectionsFactory.get_micros_per_samp_for_freq(nb.sampFreqUsed)
            maxGapToAllow = round(NcsSectionsFactory._maxGapSampFrac * 1e6 / nomFreq)
            nb = NcsSectionsFactory._parseForMaxGap(ncsMemMap, nb, maxGapToAllow)

        return nb
    
    @staticmethod
    def _buildNcsGeneric(ncsMemMap, sampFreq, gapTolerance=0):
        """
        Build 

        This replace:
        _buildGivenActualFrequency
        _buildForMaxGap
        """
        channel_id = ncsMemMap["channel_id"][0]

        ncsSects = NcsSections()

        # check if file is one block of records, which is often the case, and avoid full parse
        # need that last timestamp match the predicted one.
        predLastBlockStartTime = NcsSectionsFactory.calc_sample_time(
            sampFreq, ncsMemMap["timestamp"][0], NcsSection._RECORD_SIZE * (ncsMemMap.shape[0] - 1)
        )
        if (
            channel_id == ncsMemMap["channel_id"][-1]
            and ncsMemMap["sample_rate"][0] == ncsMemMap["sample_rate"][-1]
            and ncsMemMap["timestamp"][-1] == predLastBlockStartTime
        ):
            lastBlkEndTime = NcsSectionsFactory.calc_sample_time(sampFreq, ncsMemMap["timestamp"][-1], ncsMemMap["nb_valid"][-1])
            n_samples = NcsSection._RECORD_SIZE * (ncsMemMap.size - 1) + ncsMemMap["nb_valid"][-1]
            section0 = NcsSection(
                    startRec=0,
                    startTime=ncsMemMap["timestamp"][0],
                    endRec=ncsMemMap.size - 1,
                    endTime=lastBlkEndTime,
                    n_samples=n_samples
                )
            ncsSects.sects.append(section0)
        
        else:
            # need to parse all data block to detect gaps
            # check when the predicted timestamp is outside the tolerance
            delta = (ncsMemMap["timestamp"][1:] - ncsMemMap["timestamp"][:-1]).astype(np.int64)
            delta_prediction = ((ncsMemMap["nb_valid"][:-1] / sampFreq) * 1e6).astype(np.int64)

            gap_inds = np.flatnonzero(np.abs(delta - delta_prediction) > gapTolerance)
            gap_inds += 1

            sections_limits = [ 0 ] + gap_inds.tolist() + [len(ncsMemMap)]

            for i in range(len(gap_inds) + 1):
                start = sections_limits[i]
                stop = sections_limits[i + 1]
                duration = np.uint64(1e6 / sampFreq * ncsMemMap["nb_valid"][stop-1])
                ncsSects.sects.append(
                    NcsSection(
                        startRec=start,
                        startTime=ncsMemMap["timestamp"][start],
                        endRec=stop-1,
                        endTime=ncsMemMap["timestamp"][stop-1] + duration,
                        n_samples=np.sum(ncsMemMap["nb_valid"][start:stop])
                    )
                )

        return ncsSects

    @staticmethod
    def build_for_ncs_file(ncsMemMap, nlxHdr, gapTolerance=None, strict_gap_mode=True):
        """
        Build an NcsSections object for an NcsFile, given as a memmap and NlxHeader,
        handling gap detection appropriately given the file type as specified by the header.

        Parameters
        ----------
        ncsMemMap:
            memory map of file
        nlxHdr:
            NlxHeader from corresponding file.

        Returns
        -------
        An NcsSections corresponding to the provided ncsMemMap and nlxHdr
        """
        acqType = nlxHdr.type_of_recording()
        freq = nlxHdr["sampling_rate"]

        if acqType == "PRE4":
            # Old Neuralynx style with truncated whole microseconds for actual sampling. This
            # restriction arose from the sampling being based on a master 1 MHz clock.
            microsPerSampUsed = math.floor(NcsSectionsFactory.get_micros_per_samp_for_freq(freq))
            sampFreqUsed = NcsSectionsFactory.get_freq_for_micros_per_samp(microsPerSampUsed)
            if gapTolerance is None:
                if strict_gap_mode:
                    # this is the old behavior, maybe we could put 0.9 sample interval no ?
                    gapTolerance = 0
                else:
                    gapTolerance = 0

            ncsSects = NcsSectionsFactory._buildNcsGeneric(ncsMemMap, sampFreqUsed, gapTolerance=gapTolerance)
            ncsSects.sampFreqUsed = sampFreqUsed
            ncsSects.microsPerSampUsed = microsPerSampUsed

        elif acqType in ["DIGITALLYNX", "DIGITALLYNXSX", "CHEETAH64", "CHEETAH560", "RAWDATAFILE"]:
            # digital lynx style with fractional frequency and micros per samp determined from block times
            if gapTolerance is None:
                if strict_gap_mode:
                    # this is the old behavior
                    gapTolerance = round(NcsSectionsFactory._maxGapSampFrac * 1e6 / freq)
                else:
                    # quarter of paquet size is tolerate
                    gapTolerance = round(0.25 * NcsSection._RECORD_SIZE * 1e6 / freq)
            ncsSects = NcsSectionsFactory._buildNcsGeneric(ncsMemMap, freq, gapTolerance=gapTolerance)
            

            # take longer data block to compute reaal sampling rate
            # ind_max = np.argmax([section.n_samples for section in ncsSects.sects])
            ind_max = np.argmax([section.endRec - section.startRec for section in ncsSects.sects])
            section = ncsSects.sects[ind_max]
            if section.endRec != section.startRec:
                # need several data block
                sampFreqUsed = (
                    (section.n_samples - ncsMemMap["nb_valid"][section.endRec])
                    * 1e6
                    / (ncsMemMap["timestamp"][section.endRec] - section.startTime)
                )
            else:
                sampFreqUsed = freq
            ncsSects.sampFreqUsed = sampFreqUsed
            ncsSects.microsPerSampUsed = NcsSectionsFactory.get_micros_per_samp_for_freq(sampFreqUsed)

        
        elif acqType == "BML" or acqType == "ATLAS":
            # BML & ATLAS style with fractional frequency and micros per samp
            if strict_gap_mode:
                # this is the old behavior, maybe we could put 0.9 sample interval no ?
                gapTolerance = 0
            else:
                # quarter of paquet size is tolerate
                gapTolerance = round(0.25 * NcsSection._RECORD_SIZE * 1e6 / freq)
            ncsSects = NcsSectionsFactory._buildNcsGeneric(ncsMemMap, freq, gapTolerance=gapTolerance)
            ncsSects.sampFreqUsed = freq
            ncsSects.microsPerSampUsed = NcsSectionsFactory.get_micros_per_samp_for_freq(freq)


        else:
            raise TypeError("Unknown Ncs file type from header.")

        return ncsSects

    @staticmethod
    def _verifySectionsStructure(ncsMemMap, ncsSects):
        """
        Check that the record structure and timestamps for the ncsMemMap
        agrees with that in ncsSects.

        Provides a more rapid verification of structure than building a new NcsSections
        and checking equality.

        Parameters
        ----------
        ncsMemMap:
            memmap of file to be checked
        ncsSects
            existing block structure to be checked

        Returns
        -------
            true if all timestamps and block record starts and stops agree, otherwise false.
        """
        for blki in range(0, len(ncsSects.sects)):
            if ncsMemMap["timestamp"][ncsSects.sects[blki].startRec] != ncsSects.sects[blki].startTime:
                return False
            ets = ncsMemMap["timestamp"][ncsSects.sects[blki].endRec]
            enb = ncsMemMap["nb_valid"][ncsSects.sects[blki].endRec]
            endTime = NcsSectionsFactory.calc_sample_time(ncsSects.sampFreqUsed, ets, enb)
            if endTime != ncsSects.sects[blki].endTime:
                return False

        return True
