#ifndef DISPATCHER_H
#define DISPATCHER_H

#include <map>
#include <iostream>
#include <string>
#include <unordered_map>
#include "CGSim.h"

class DISPATCHER
{

public:
  DISPATCHER(){};
 ~DISPATCHER(){};

  double      storage_needed(std::unordered_map<std::string, long long>& files);
  long long   storage_required_for_site(Job* j, const std::string& site);
  void        commitStorage(Job* job, unsigned long long bytes);
  void        commitOutputWrite(Job* job, unsigned long long bytes);
  void        releaseJobStorage(Job* job);
  Job*        assignJob(Job* job);
  void        findBestSite(Job* j);  
  void        findAvailableCPU(Job* j);

private:
  std::unordered_map<std::string, unsigned long long> site_reserved_;
  std::unordered_map<long long, unsigned long long> job_reserved_;
  std::unordered_map<long long, int> output_writes_remaining_;
};

#endif
