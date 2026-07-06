#include "output.h"
#include <filesystem>

void OUTPUT::initialize()
{
    if (initialized) return;
    std::string file_name = platform->get_property("output_file");
    if (std::filesystem::exists(file_name)) std::filesystem::remove(file_name);

    if (sqlite3_open(file_name.c_str(), &db) != SQLITE_OK) {
        throw std::invalid_argument("SQLite file " + file_name + " cannot be opened.");
    }

    if (SQLITE_OK != sqlite3_exec(db, "PRAGMA journal_mode=WAL;", nullptr, 0, nullptr)) {
        throw std::runtime_error("Failed to set database connection in WAL mode.");
    }
    initialized = true;
    createEventsTable();
}

void OUTPUT::createEventsTable()
{
    const char* create_stmt =
        "CREATE TABLE EVENTS ("
        "_ID INTEGER PRIMARY KEY AUTOINCREMENT, "
        "EVENT TEXT NOT NULL, "
        "STATE TEXT NOT NULL, "
        "STATUS TEXT NOT NULL, "
        "JOB_ID TEXT NOT NULL, "
        "TIME FLOAT NOT NULL, "
        "METADATA TEXT"
        ");";


    char* errmsg = nullptr;
    int ret = sqlite3_exec(db, create_stmt, nullptr, nullptr, &errmsg);
    if (ret != SQLITE_OK) {
        sqlite3_free(errmsg);
        throw std::runtime_error("Database table creation failed");
    }
}

void OUTPUT::insert_event(
                  const std::string& event,
                  const std::string& state,
                  const std::string& job_id,
                  const std::string& status,
                  double time,
                  const std::string& payload)
{
    sqlite3_stmt* stmt;
    std::string sql_insert =
        "INSERT INTO EVENTS (EVENT, STATE, JOB_ID, STATUS, TIME, METADATA) VALUES (?, ?, ?, ?, ?, ?)";

    int rc = sqlite3_prepare_v2(db, sql_insert.c_str(), -1, &stmt, nullptr);
    if (rc != SQLITE_OK)
        throw std::runtime_error(std::string("SQLite prepare failed: ") + sqlite3_errmsg(db));

    sqlite3_bind_text(stmt, 1, event.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(stmt, 2, state.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(stmt, 3, job_id.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(stmt, 4, status.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_double(stmt, 5, time);
    sqlite3_bind_text(stmt, 6, payload.c_str(), -1, SQLITE_TRANSIENT);

    if (sqlite3_step(stmt) != SQLITE_DONE)
    {
        sqlite3_finalize(stmt);
        throw std::runtime_error(std::string("SQLite step failed: ") + sqlite3_errmsg(db));
    }

    sqlite3_finalize(stmt);
}


void OUTPUT::onSimulationStart()
{

}

void OUTPUT::onSimulationEnd()
{

}

void OUTPUT::onJobTransferStart(Job* job, sg4::Mess const& me)
{
    json payload = {
        {"site", job->comp_site},
        {"host", job->comp_host}
    };

    insert_event("JobAllocation", "Started",
                 std::to_string(job->jobid),
                 job->status,
                 sg4::Engine::get_clock(),
                 payload.dump());
}

void OUTPUT::onJobTransferEnd(Job* job, sg4::Mess const& me)
{
    json payload = {
        {"site", job->comp_site},
        {"host", job->comp_host},
        {"site_storage_util", calculate_site_storage_util(job->comp_site)},
        {"grid_storage_util", calculate_grid_storage_util()},
        {"site_cpu_util", calculate_site_cpu_util(job->comp_site)},
        {"grid_cpu_util", calculate_grid_cpu_util()}
    };

    insert_event("JobAllocation", "Finished",
                 std::to_string(job->jobid),
                 job->status,
                 sg4::Engine::get_clock(),
                 payload.dump());
}

void OUTPUT::onJobExecutionStart(Job* job, sg4::Exec const& ex)
{
    json payload = {
        {"flops", job->flops},
        {"site", job->comp_site},
        {"host", job->comp_host},
        {"cores", job->cores},
        {"speed", job->comp_host_speed},
        {"site_cpu_util", calculate_site_cpu_util(job->comp_site)},
        {"grid_cpu_util", calculate_grid_cpu_util()}
    };

    insert_event("JobExecution", "Started",
                 std::to_string(job->jobid),
                 job->status,
                 ex.get_start_time(),
                 payload.dump());
}

void OUTPUT::onJobExecutionEnd(Job* job, sg4::Exec const& ex)
{
    json payload = {
        {"flops", job->flops},
        {"cores", job->cores},
        {"site", job->comp_site},
        {"host", job->comp_host},
        {"speed", job->comp_host_speed},
        {"cost", ex.get_cost()},
        {"site_cpu_util", calculate_site_cpu_util(job->comp_site)},
        {"grid_cpu_util", calculate_grid_cpu_util()},
        {"duration", ex.get_finish_time() - ex.get_start_time()},
        {"retries", job->retries},
        {"total_io_read_time", job->total_io_read_time},
        {"file_transfer_queue_time", job->file_transfer_queue_time},
        {"resource_waiting_queue_time", job->resource_waiting_queue_time},
        {"total_queue_time", job->file_transfer_queue_time+job->resource_waiting_queue_time},
    };

    insert_event("JobExecution", "Finished",
                 std::to_string(job->jobid),
                 job->status,
                 ex.get_finish_time(),
                 payload.dump());
}

void OUTPUT::onFileTransferStart(Job* job,
                                 const std::string& filename,
                                 const unsigned long long filesize,
                                 sg4::Comm const& co,
                                 const std::string& src_site,
                                 const std::string& dst_site)
{
    auto link = get_link(src_site, dst_site);

    json payload = {
        {"file", filename},
        {"size", filesize},
        {"source_site", src_site},
        {"destination_site", dst_site},
        {"bandwidth", link->get_bandwidth()},
        {"latency", link->get_latency()},
        {"link_load", link->get_load()},
        {"site_storage_util", calculate_site_storage_util(job->comp_site)},
        {"grid_storage_util", calculate_grid_storage_util()}
    };

    insert_event("FileTransfer", "Started",
                 std::to_string(job->jobid),
                 job->status,
                 co.get_start_time(),
                 payload.dump());
}

void OUTPUT::onFileTransferEnd(Job* job,
                               const std::string& filename,
                               const unsigned long long filesize,
                               sg4::Comm const& co,
                               const std::string& src_site,
                               const std::string& dst_site)
{
    auto link = get_link(src_site, dst_site);

    json payload = {
        {"file", filename},
        {"size", filesize},
        {"source_site", src_site},
        {"destination_site", dst_site},
        {"duration", co.get_finish_time() - co.get_start_time()},
        {"bandwidth", link->get_bandwidth()},
        {"latency", link->get_latency()},
        {"link_load", link->get_load()},
        {"site_storage_util", calculate_site_storage_util(job->comp_site)},
        {"grid_storage_util", calculate_grid_storage_util()}
    };

    insert_event("FileTransfer", "Finished",
                 std::to_string(job->jobid),
                 job->status,
                 co.get_finish_time(),
                 payload.dump());
}

void OUTPUT::onFileReadStart(Job* job,
                            const std::string& filename,
                            const unsigned long long filesize,
                            sg4::Io const& io)
{
    json payload = {
        {"file", filename},
        {"size", filesize},
        {"site", job->comp_site},
        {"host", job->comp_host},
        {"disk", job->disk},
        {"disk_read_bw", job->disk_read_bw}
    };

    insert_event("FileRead", "Started",
                 std::to_string(job->jobid),
                 job->status,
                 io.get_start_time(),
                 payload.dump());
}

void OUTPUT::onFileReadEnd(Job* job,
                           const std::string& filename,
                           const unsigned long long filesize,
                           sg4::Io const& io)
{
    json payload = {
        {"file", filename},
        {"size", filesize},
        {"site", job->comp_site},
        {"host", job->comp_host},
        {"disk", job->disk},
        {"disk_read_bw", job->disk_read_bw},
        {"duration", io.get_finish_time() - io.get_start_time()}
    };

    insert_event("FileRead", "Finished",
                 std::to_string(job->jobid),
                 job->status,
                 io.get_finish_time(),
                 payload.dump());
}

void OUTPUT::onFileWriteStart(Job* job,
                             const std::string& filename,
                             const unsigned long long filesize,
                             sg4::Io const& io)
{
    json payload = {
        {"file", filename},
        {"size", filesize},
        {"site", job->comp_site},
        {"host", job->comp_host},
        {"disk", job->disk},
        {"disk_write_bw", job->disk_write_bw},
        {"site_storage_util", calculate_site_storage_util(job->comp_site)},
        {"grid_storage_util", calculate_grid_storage_util()}
    };

    insert_event("FileWrite", "Started",
                 std::to_string(job->jobid),
                 job->status,
                 io.get_start_time(),
                 payload.dump());
}

void OUTPUT::onFileWriteEnd(Job* job,
                           const std::string& filename,
                           const unsigned long long filesize,
                           sg4::Io const& io)
{
    json payload = {
        {"file", filename},
        {"size", filesize},
        {"site", job->comp_site},
        {"host", job->comp_host},
        {"duration", io.get_finish_time() - io.get_start_time()},
        {"disk", job->disk},
        {"disk_write_bw", job->disk_write_bw},
        {"site_storage_util", calculate_site_storage_util(job->comp_site)},
        {"grid_storage_util", calculate_grid_storage_util()}
    };

    insert_event("FileWrite", "Finished",
                 std::to_string(job->jobid),
                 job->status,
                 io.get_finish_time(),
                 payload.dump());
}


void OUTPUT::onBackGroundFileTransferStart(const std::string& filename, 
    const unsigned long long filesize, simgrid::s4u::Comm const& co, 
    const std::string& src_site, const std::string& dst_site, const std::string& policy_name)
{

    auto link = get_link(src_site, dst_site);

    json payload = {
        {"Policy", policy_name},
        {"file", filename},
        {"size", filesize},
        {"source_site", src_site},
        {"destination_site", dst_site},
        {"bandwidth", link->get_bandwidth()},
        {"latency", link->get_latency()},
        {"link_load", link->get_load()},
        {"src_site_storage_util", calculate_site_storage_util(src_site)},
        {"dst_site_storage_util", calculate_site_storage_util(dst_site)},
        {"grid_storage_util", calculate_grid_storage_util()}
    };

    insert_event("BackGroundFileTransfer", "Started",
                 "",
                 "",
                 co.get_start_time(),
                 payload.dump());


}

void OUTPUT::onBackGroundFileTransferEnd(const std::string& filename, 
    const unsigned long long filesize, simgrid::s4u::Comm const& co, 
    const std::string& src_site, const std::string& dst_site, const std::string& policy_name)
{
    auto link = get_link(src_site, dst_site);

    json payload = {
        {"Policy", policy_name},
        {"file", filename},
        {"size", filesize},
        {"source_site", src_site},
        {"destination_site", dst_site},
        {"duration", co.get_finish_time() - co.get_start_time()},
        {"bandwidth", link->get_bandwidth()},
        {"latency", link->get_latency()},
        {"link_load", link->get_load()},
        {"src_site_storage_util", calculate_site_storage_util(src_site)},
        {"dst_site_storage_util", calculate_site_storage_util(dst_site)},
        {"grid_storage_util", calculate_grid_storage_util()}
    };

    insert_event("BackGroundFileTransfer", "Finished",
                 "",
                 "",
                 co.get_finish_time(),
                 payload.dump());

}

sg4::Link* OUTPUT::get_link(const std::string& src_site, const std::string& dst_site)
{

    sg4::Link* link = sg4::Link::by_name_or_null("link_" + src_site + ":" + dst_site);
    if (!link) link = sg4::Link::by_name_or_null("link_" + dst_site + ":" + src_site);
    if (!link) throw std::runtime_error("Link not found");
    return link;
}

double OUTPUT::calculate_grid_cpu_util()
{
    double cores_used = 0;
    double total_cores = std::stoul(platform->get_property("grid_cores"));
    for (const auto& host : sg4::Engine::get_instance()->get_all_hosts()) 
    {
        if(host->get_name().find("JOB-SERVER_cpu") != std::string::npos) continue;
        if(host->get_name().find("_communication") != std::string::npos) continue;
        cores_used += host->extension<HostExtensions>()->get_cores_used();
    }
    return cores_used/total_cores;
}

double OUTPUT::calculate_site_cpu_util(const std::string& site_name)
{
    auto site = sg4::Engine::get_instance()->netzone_by_name_or_null(site_name);
    double total_cores = std::stoul(site->get_property("total_cores"));
    double cores_used = 0;
    for (const auto& host : site->get_all_hosts()) 
    {
        if(host->get_name().find("_communication") != std::string::npos) continue;
        cores_used += host->extension<HostExtensions>()->get_cores_used();
    }
    return cores_used/total_cores;
}

double OUTPUT::calculate_grid_storage_util()
{
    double total_storage = std::stoull(platform->get_property("grid_storage"));
    double remaining_storage = CGSim::get_file_manager()->request_remaining_grid_storage();
    return (1.0-remaining_storage/total_storage);
}

double OUTPUT::calculate_site_storage_util(const std::string& site_name)
{
    auto   site = sg4::Engine::get_instance()->netzone_by_name_or_null(site_name);
    double total_storage = std::stoull(site->get_property("storage_capacity_bytes"));
    double remaining_storage = CGSim::get_file_manager()->request_remaining_site_storage(site_name);
    return (1.0-remaining_storage/total_storage);
}

