package pkg_b;
  class agent;
    int total;
    virtual task run_phase();
      total--;
    endtask
    function void write(int v);
      total -= v;
    endfunction
  endclass
endpackage
