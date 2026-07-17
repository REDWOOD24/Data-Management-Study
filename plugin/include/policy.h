#ifndef POLICY_H
#define POLICY_H

#include "CGSim.h"
#include "output.h"

#include <algorithm>
#include <limits>
#include <memory>
#include <random>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace sg4 = simgrid::s4u;

class POLICY {
public:
    POLICY() = default;
    ~POLICY() = default;

    /* Most of the structure of this policy is for proactive running in the background once configured. */
    
    /* A policy file is required. The returned policy_content is an optional json object. */
    void configurePolicy(const std::string& policy_file);

    /* One-shot "drop-in" proactive transfers scheduled at explicit timestamps.
       Loaded from a standalone config file referenced by
       Data_Management_Policy.drop_in_transfers_file, independent of the
       reactive/proactive policy configuration. */
    struct DropInTransfer {
        double time = 0.0;
        std::string filename;
        std::string src_site;
        std::string dst_site;
        CGSim::FileTransferDecisionMode mode = CGSim::FileTransferDecisionMode::COPY;
    };

    /* Reactive transfer on demand. */
    void onFileRequest(Job* j, std::string filename, long long filesize, std::unordered_set<std::string> file_locations, std::string& source_site, CGSim::FileTransferDecisionMode& mode);

private:
    json policy_content;     // policy content

    enum class FilePickMode {
        FIRST_FIT,
        LARGEST_FIT,
        SMALLEST_FIT,
        RANDOM_FIT
    };

    enum class PathMetricMode {
        ESTIMATED_TRANSFER_TIME,
        LINK_LOAD,
        BANDWIDTH_ONLY
    };

    enum class CandidateDestinationPolicy {
        REQUESTING_SITES_FIRST,
        LEAST_UTILIZED_AMONG_REQUESTING
    };

    struct SiteUtil {
        std::string site;
        double util = 0.0;
        unsigned long long remaining = 0;
    };

    struct Candidate {
        std::string filename;
        std::string src_site;
        std::string dst_site;
        unsigned long long size = 0;
        double link_load = 0.0;
        double bandwidth = 0.0;
        double latency = 0.0;
        double estimated_time = 0.0;
    };

    CGSim::Policy* storage_rebalance_policy(
        float interval,
        double high_utilization_threshold,
        double low_utilization_threshold,
        int max_transfers_per_tick,
        FilePickMode file_pick,
        bool skip_if_already_replica_on_destination,
        CGSim::FileTransferDecisionMode mode
    );
    CGSim::Policy* network_aware_rebalance_policy(
        float interval,
        double high_utilization_threshold,
        double low_utilization_threshold,
        double max_path_load,
        int max_transfers_per_tick,
        FilePickMode file_pick,
        PathMetricMode path_metric,
        CGSim::FileTransferDecisionMode mode
    );
    CGSim::Policy* hotset_replication_policy(
        float interval,
        float hotness_window,
        double hotness_threshold,
        float prediction_horizon,
        int target_replica_count,
        CandidateDestinationPolicy candidate_destination_policy,
        int max_transfers_per_tick,
        CGSim::FileTransferDecisionMode mode
    );
    CGSim::Policy* custom_policy_agent_policy();

    /* Schedule the drop-in transfers listed in the standalone config file (if any). */
    void configure_drop_in_transfers(const std::string& policy_file);

    /* Execute a single drop-in transfer. Returns true when the background
       transfer was started; false when it was skipped (e.g. the file is not
       at the source site at this moment), in which case the request is ignored. */
    bool run_drop_in_transfer(const DropInTransfer& transfer, const std::string& policy_name);

    void run_storage_rebalance(
        const std::string& policy_name,
        double high_utilization_threshold,
        double low_utilization_threshold,
        int max_transfers_per_tick,
        FilePickMode file_pick,
        bool skip_if_already_replica_on_destination,
        CGSim::FileTransferDecisionMode mode
    );

    void run_network_aware_rebalance(
        const std::string& policy_name,
        double high_utilization_threshold,
        double low_utilization_threshold,
        double max_path_load,
        int max_transfers_per_tick,
        FilePickMode file_pick,
        PathMetricMode path_metric,
        CGSim::FileTransferDecisionMode mode
    );

    void run_hotset_replication(
        const std::string& policy_name,
        double hotness_threshold,
        int target_replica_count,
        int max_transfers_per_tick,
        CGSim::FileTransferDecisionMode mode
    );

    std::vector<SiteUtil> get_site_utils() const;

    bool choose_file_from_site(
        const std::string& src_site,
        const std::string& dst_site,
        unsigned long long dst_remaining,
        FilePickMode file_pick,
        bool skip_if_already_replica_on_destination,
        std::string& filename,
        unsigned long long& filesize
    );

    sg4::Link* link_between_sites(
        const std::string& src_site,
        const std::string& dst_site
    ) const;

    bool site_has_file(
        const std::string& site,
        const std::string& filename
    ) const;

    bool is_file_in_flight_to_site(
        const std::string& filename,
        const std::string& dst_site
    ) const;

    void finalize_reactive_source(
        Job* j,
        const std::string& filename,
        std::string& source_site
    ) const;

    bool try_background_transfer(
        const std::string& filename,
        const std::string& src_site,
        const std::string& dst_site,
        CGSim::FileTransferDecisionMode mode,
        const std::string& policy_name
    );


private:
    std::mt19937 rng{1337};
};

#endif