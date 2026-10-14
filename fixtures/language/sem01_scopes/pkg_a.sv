package pkg_a;
  class agent;
    int count;
    virtual task run_phase();
      count++;
    endtask
    function void write(int v);
      count += v;
    endfunction
  endclass
endpackage
