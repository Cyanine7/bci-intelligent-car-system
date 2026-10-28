#ifndef __BSP_SIIC_H
#define __BSP_SIIC_H

#include "sys.h"
#include "icm20948.h"

typedef enum 
{
	IIC_OK       = 0x00U,
	IIC_ERR      = 0x01U,
	IIC_BUSY     = 0x02U,
	IIC_TIMEOUT  = 0x03U,
	I2C_BUS_ERROR,
	I2C_ARBITRATION_LOST,
	I2C_ADDR_NACK
} IIC_Status_t;


typedef struct {
	void (*init)(void);
	//参数：设备地址，要写入的数据，要写入的数据长度，超时时间
    IIC_Status_t (*write)(uint16_t DevAddress, uint8_t *pData, uint16_t Size, uint32_t Timeout);
    IIC_Status_t (*read)(uint16_t DevAddress, uint8_t *pData, uint16_t Size, uint32_t Timeout);
	
	//参数：设备地址，要访问的寄存器地址，要写入的数据，要写入的数据长度，超时时间
    IIC_Status_t (*write_reg)(uint16_t DevAddress, uint16_t MemAddress, uint8_t *pData, uint16_t Size, uint32_t Timeout);
	IIC_Status_t (*read_reg)(uint16_t DevAddress, uint16_t MemAddress,  uint8_t *pData, uint16_t Size, uint32_t Timeout);
	
	void (*delay_ms)(uint16_t ms);
}IICInterface_t,*pIICInterface_t;

extern IICInterface_t User_sIICDev;


// 这三个函数供 icm20948 驱动调用，内部使用 User_sIICDev 完成读写
icm20948_status_e icm20948_internal_write_i2c(uint8_t reg, uint8_t *data, uint32_t len, void *user);
icm20948_status_e icm20948_internal_read_i2c(uint8_t reg, uint8_t *buff, uint32_t len, void *user);

// 初始化 ICM20948 的 I2C 接口（绑定 serif 到 device）
void icm20948_init_i2c(icm20948_device_t *icm_device);


#endif /* __BSP_SIIC_H */

