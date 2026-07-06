#ifndef WORKLOAD_MANAGER_H
#define WORKLOAD_MANAGER_H

#include "CGSim.h"
#include <fstream>
#include <sstream>

class WORKLOAD_MANAGER {

public:
    WORKLOAD_MANAGER(){};
   ~WORKLOAD_MANAGER(){};
    Job* createJob();
    JobQueue getWorkload();


private:
   long long random_number(long long min, long long max);
   static int JOB_ID;
   
};


#endif //WORKLOAD_MANAGER_H
