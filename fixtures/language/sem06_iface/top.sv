interface bus_if;
  logic [7:0] data;
  logic       valid;
  modport mp_src(output data, valid);
  modport mp_dst(input data, valid);
endinterface

module src_m(bus_if.mp_src b);
  assign b.data  = 8'h5A;
  assign b.valid = 1'b1;
endmodule

module dst_m(bus_if.mp_dst b, output logic ok);
  assign ok = b.valid && (b.data != 8'h00);
endmodule

module sem06_top(output logic ok_a, output logic ok_b);
  bus_if u_bus_a();
  bus_if u_bus_b();
  src_m u_src_a(.b(u_bus_a));
  src_m u_src_b(.b(u_bus_b));
  dst_m u_dst_a(.b(u_bus_a), .ok(ok_a));
  dst_m u_dst_b(.b(u_bus_b), .ok(ok_b));

  // A virtual interface handle is only a typed candidate: nothing here binds it to u_bus_a/u_bus_b.
  class vif_drv;
    virtual bus_if.mp_src vif;
  endclass
  vif_drv drv_h = new();
endmodule
